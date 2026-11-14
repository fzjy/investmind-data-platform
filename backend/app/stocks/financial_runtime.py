"""只提升当前范围的财务报表工作区；其他产品及工作台区域不变。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone
from contextlib import ExitStack
from sqlalchemy import select,text
from app.db.models import CashflowStatement, PublishedSnapshot
from app.stocks.derived.inputs import FinancialPoint
from app.jobs.locks import job_lock_for_engine,data_publication_lock_key
from app.marts.contracts import MartCandidate,MartRows,QualityContext,release_version
from app.marts.quality import QualityGate
from app.marts.publication import SnapshotPublisher,PublicationBaseChanged
from app.stocks.financial_supply import config,capture,load_points,statement_tables,_base
from app.stocks.marts.metric_cards import MetricCardSourceBusy


def workbench_points(factory, as_of, symbols, *, allow_new_price_day=False):
    points = load_points(factory, as_of, symbols, allow_new_price_day=allow_new_price_day)
    if not points:
        return ()
    # 历史补采与披露窗口可以取得同一报告的不同字段，也可能只有日常路径拿到了新一期。
    with factory() as session:
        daily = session.scalars(select(CashflowStatement).where(
            CashflowStatement.ts_code.in_(symbols), CashflowStatement.end_date <= as_of,
            CashflowStatement.report_type == '1'))
        core = [FinancialPoint(row.end_date, row.f_ann_date or row.ann_date, row.report_type,
                    {**(row.data or {}), 'ann_date': row.ann_date, 'update_flag': row.update_flag}, row.ts_code,
                    'core.cashflow_statement', f'core.cashflow_statement:{row.id}')
                for row in daily]
    return (*points, *core)


def tables_for_build(factory,as_of,symbols):
    if factory is None:return {}
    try:points=workbench_points(factory,as_of,symbols,allow_new_price_day=True)
    except ValueError as exc:
        if str(exc)!='FINANCIAL_BATCH_INCOMPLETE':raise
        points=()
    if points:return {symbol:statement_tables([p for p in points if p.ts_code==symbol],as_of) for symbol in symbols}
    # 新价格窗可暂时保留上次已发布的财务证据；不丢扩展字段，不冒称新采集。
    with factory() as s:
        return {p.payload['symbol']:deepcopy(p.payload['financial']['statements']['tables']) for p in s.scalars(select(PublishedSnapshot).where(PublishedSnapshot.product.in_(['stock-workbench:'+v for v in symbols])))
            if p.as_of.date()<=as_of and p.payload.get('financial',{}).get('statements',{}).get('dataSupply')}


def patch_workspace(financial,tables):
    result=deepcopy(financial)
    result['statements']['tables']=tables
    result['statements']['dataSupply']='verified-raw-statements'
    result['years']=sorted({p for t in tables for p in t['periods']})
    result['reportTypes']=['1']
    result['announcementDate']=max((m['announcementDate'] for t in tables for m in t.get('reportMetadata',[])),default='')
    return result


def publish(factory,*,dry_run=False):
    lock=job_lock_for_engine(factory.kw['bind']);key=data_publication_lock_key()
    acquired=not dry_run and lock.try_acquire(key)
    report=dict(status='blocked',requestCount=0,promotedProducts=[])
    if not dry_run and not acquired:return report|{'code':'FINANCIAL_PUBLICATION_LOCK_BUSY'}
    try:
        with factory() as s:
            if factory.kw['bind'].dialect.name=='postgresql':s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            base=_base(s);as_of=datetime.fromisoformat(base['asOf']).date()
            screen=s.get(PublishedSnapshot,'screener');previous={'screener':screen.version};freshness={'screener':(screen.stale,screen.stale_reason)}
            old={symbol:s.get(PublishedSnapshot,'stock-workbench:'+symbol) for symbol in base['symbols']}
            if any(p is None or p.as_of.date()!=as_of for p in old.values()):return report|{'code':'FINANCIAL_WORKBENCH_BASE_REQUIRED'}
            old={k:deepcopy(v) for k,v in old.items()}
        points=workbench_points(factory,as_of,base['symbols'])
        if not points:return report|{'code':'FINANCIAL_EVIDENCE_REQUIRED'}
        candidates=[]
        for symbol,old_row in old.items():
            tables=statement_tables([p for p in points if p.ts_code==symbol],as_of)
            if any(not t['periods'] for t in tables):return report|{'code':'FINANCIAL_REPORT_TABLE_EMPTY','symbol':symbol}
            payload=deepcopy(old_row.payload);payload['financial']=patch_workspace(payload['financial'],tables)
            previous[old_row.product]=old_row.version;freshness[old_row.product]=(old_row.stale,old_row.stale_reason)

            candidates.append(MartCandidate(old_row.product,old_row.as_of,payload,MartRows(workbenches=(payload,),charts=tuple(payload['dailyChart']['candles'])),stale=old_row.stale,stale_reason=old_row.stale_reason))
        from app.stocks.marts.stocks import project_workbench_cards
        candidates = project_workbench_cards(factory, candidates)
        changed = any(c.payload != old[c.payload['symbol']].payload for c in candidates)
        # 此批不重建选股器，eligible=0；工作台范围仍显式列出并检查。
        checks=QualityGate().evaluate(tuple(candidates),QualityContext(0,tuple(base['symbols']),0,0,()))
        failures=[c.name for c in checks if c.severity=='blocking' and not c.passed]
        if failures:return report|dict(code='FINANCIAL_QUALITY_BLOCKED',failedChecks=failures)
        if dry_run:return report|dict(status='planned',wouldPublish=changed)
        if not changed:return report|dict(status='no_change',symbolCount=len(base['symbols']))
        version=release_version(as_of,candidates)
        result=SnapshotPublisher(factory).publish_many(candidates,version,checks,expected_versions=previous,expected_freshness=freshness)
        return report|dict(status='completed',version=version,symbolCount=len(base['symbols']),promotedProducts=list(result.promoted_products))
    except MetricCardSourceBusy:return report|dict(status='locked',code='WORKBENCH_CARD_SOURCE_BUSY_RETRY',retryable=True)
    except PublicationBaseChanged:return report|{'code':'FINANCIAL_BASE_CHANGED'}
    except (ValueError,KeyError,TypeError):return report|{'code':'FINANCIAL_SOURCE_BLOCKED'}
    finally:
        if acquired:lock.release(key)


def run_financial_step(settings,*,now=None,dry_run=False,publish_requested=False):
    from app.db.session import create_engine_and_session
    from app.shared.readonly_database import open_readonly_factory
    from app.connectors.tushare.client import TushareClient
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    policy=config()
    if not policy['enabled']:return dict(status='not_selected',requestCount=0)
    with ExitStack() as resources:
        if dry_run:factory=resources.enter_context(open_readonly_factory(settings.database_url))
        else:
            engine,factory=create_engine_and_session(settings.database_url);resources.callback(engine.dispose)
        result=capture(factory,execute=False,now=now)
        if dry_run or result['status']!='planned':return result
        client=None
        if result['completedCount']<result['unitCount']:
            if settings.tushare_token is None:return dict(status='configuration_error',requestCount=0)
            rate=settings.tushare_vip_requests_per_minute if settings.tushare_mode=='vip' else settings.tushare_standard_requests_per_minute
            client=resources.enter_context(TushareClient(settings.tushare_token.get_secret_value(),max_retries=0,timeout_seconds=policy['timeoutSeconds'],rate_limiter=RequestRateLimiter(rate)))
        result=capture(factory,client,execute=True,now=now)
        result['publishReady']=result['status']=='completed' and result['compatibleTarget']
        if result['publishReady'] and publish_requested:
            result['publication']=publish(factory)
            if result['publication']['status'] not in ('completed','no_change'):result['status']='partial'
        return result
