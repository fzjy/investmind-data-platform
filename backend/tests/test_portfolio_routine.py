"""日常组合组：真实SQLite和HTTP边界替身，不接供应商。"""
from datetime import datetime, timedelta, timezone
from copy import deepcopy
import pytest
from sqlalchemy import select, func

from app.db.models import JobCursor, SourcePayload, PublishedSnapshot, Dividend
from backend.tests.test_dividend_evidence_capture import source, Client, config, db_state

NOW = datetime(2026, 11, 5, tzinfo=timezone.utc)


def module():
    from app.portfolio import routine
    return routine


class ObservedClient(Client):
    def call(self, api, params, fields):
        # 使用真实HTTP字节形式；新namespace的持久预留由before验证。
        import json, base64, hashlib
        from app.connectors.tushare.contracts import TushareResponse
        self.before()
        self.calls.append((api, deepcopy(params), tuple(fields)))
        row = dict.fromkeys(fields)
        row.update(ts_code=params['ts_code'], end_date='20251231', ann_date='20260501',
                   div_proc='实施', cash_div_tax=1, stk_div=0, stk_bo_rate=0, stk_co_rate=0,
                   record_date='20260601', ex_date='20260602', pay_date='20260603', imp_ann_date='20260531')
        data = dict(fields=list(fields), items=[[row[f] for f in fields]])
        body = json.dumps({'code': 0, 'data': data}).encode()
        self.observer(dict(statusCode=200, credentialsRedacted=False, bodyBase64=base64.b64encode(body).decode(),
                           bodySha256=hashlib.sha256(body).hexdigest(), byteLength=len(body)))
        return TushareResponse.model_validate(data)

    def set_response_observer(self, callback):
        self.observer = callback


def refresh(factory, client=None, **kw):
    instant=kw.pop('now', NOW)
    return module().refresh_dividends(factory, client, now=instant,
        execute=kw.pop('execute', True), config=config(kw.pop('budget', 20)), revision_days=7,completion_clock=lambda:instant, **kw)


def test_preview_never_writes_and_dynamic_scope():
    f=source(); before=db_state(f)
    r=refresh(f, execute=False)
    assert r['status']=='planned' and r['requestCount']==0 and r['pendingCount']==1
    assert db_state(f)==before


def test_completed_reentry_zero_and_changed_core_refreshes_one_security():
    f=source(); c=ObservedClient(f)
    assert refresh(f,c)['requestCount']==1
    after=db_state(f)
    assert refresh(f,c)['requestCount']==0
    assert db_state(f)==after
    with f.begin() as s:
        symbol=s.get(PublishedSnapshot,'screener').payload['stocks'][0]['symbol']
        s.add(Dividend(ts_code=symbol,data={'revision-test':'new'}))
    assert refresh(f,c)['requestCount']==1
    assert len(c.calls)==2


def test_revision_interval_and_cross_day_partial_resume_do_not_repeat_completed():
    f=source()
    with f.begin() as s:
        base=s.get(PublishedSnapshot,'screener')
        base.payload={**base.payload,'stocks':[*base.payload['stocks'],{'symbol':'600002.SH'}]}
    c=ObservedClient(f)
    first=refresh(f,c,budget=1)
    assert first['status']=='pending' and first['requestCount']==1
    second=refresh(f,c,budget=1,now=NOW+timedelta(days=4))
    assert second['status']=='completed' and second['requestCount']==1
    assert len({p[1]['ts_code'] for p in c.calls})==2
    assert refresh(f,c,now=NOW+timedelta(days=6))['requestCount']==0
    assert refresh(f,c,now=NOW+timedelta(days=12))['requestCount']==2


def test_failed_cycle_blocks_new_date_and_preserves_manifest():
    f=source(); c=ObservedClient(f)
    assert refresh(f,c)['status']=='completed'
    class Broken(ObservedClient):
        def call(self,*args):
            self.before(); self.calls.append(args); raise TimeoutError('private-token')
    bad=Broken(f)
    first=refresh(f,bad,now=NOW+timedelta(days=8))
    assert first['status']=='blocked' and first['requestCount']==1
    second=refresh(f,bad,now=NOW+timedelta(days=20))
    assert second['status']=='blocked' and second['requestCount']==0 and len(bad.calls)==1
    assert 'private-token' not in str(second)


def test_corrupt_manifest_source_does_not_fallback_or_refetch():
    f=source(); c=ObservedClient(f); refresh(f,c)
    with f.begin() as s:
        raw=s.scalars(select(SourcePayload).order_by(SourcePayload.id.desc())).first()
        raw.payload={**raw.payload,'items':[]}
    r=refresh(f,c,now=NOW+timedelta(days=9))
    assert r['status']=='blocked' and r['requestCount']==0 and len(c.calls)==1


def test_routine_invokes_portfolio_after_stock_publication_and_counts_http(monkeypatch):
    from backend.tests.jobs.test_routine_update import setup_steps,settings
    m,calls=setup_steps(monkeypatch)
    monkeypatch.setattr(m,'portfolio_routine_config',lambda: {'enabled':True})
    def step(*a,**kw):
        calls.append(('portfolio',kw['now'],kw['publish']))
        return {'status':'completed','publishReady':True,'requestCount':2,'publication':{'status':'no_change'}}
    monkeypatch.setattr(m,'run_portfolio_step',step)
    r=m.run_routine_update(settings(),now=NOW,publish=True,macro_rebuild=lambda s:type('R',(),{'__dataclass_fields__':{}})())
    assert calls[1][0]=='portfolio'
    assert r['httpCounts']['portfolio']==2 and r['httpCounts']['total']==7
    assert r['portfolio']['publication']['status']=='no_change'


def test_new_workbench_model_refreshes_portfolio_in_same_routine(monkeypatch):
    from backend.tests.jobs.test_routine_update import setup_steps, settings
    from app.portfolio.marts import portfolio_runtime
    from app.stocks.marts import metric_cards
    module, _ = setup_steps(monkeypatch)
    steps = []
    monkeypatch.setattr(module, 'portfolio_routine_config', lambda: {'enabled': True})
    monkeypatch.setattr(module, 'run_portfolio_step', lambda *a, **kw: {
        'status':'completed', 'requestCount':0, 'dividends':{'publishReady':True}, 'publication':{'status':'no_change'}})
    def publish_cards(*a, **kw):
        steps.append('cards')
        return {'status':'completed'}
    def publish_portfolio(*a, **kw):
        steps.append('portfolio')
        return {'status':'completed', 'version':'refreshed'}
    monkeypatch.setattr(metric_cards, 'rebuild_metric_cards', publish_cards)
    monkeypatch.setattr(portfolio_runtime, 'rebuild_portfolio', publish_portfolio)
    report = module.run_routine_update(settings(), now=NOW, publish=True, macro_rebuild=lambda _: None)
    assert steps == ['cards','portfolio']
    assert report['portfolio']['publication']['version'] == 'refreshed'


def test_manifest_feeds_ledger_and_corruption_never_falls_back():
    from app.portfolio.marts.portfolio_ledger import load_portfolio_dividend_ledger
    from datetime import date
    f=source(); c=ObservedClient(f); refresh(f,c)
    with f() as s:
        symbol=s.get(PublishedSnapshot,'screener').payload['stocks'][0]['symbol']
    ledger=load_portfolio_dividend_ledger(f,(symbol,),date(2026,8,24),{symbol:'2026-01-01'})
    assert ledger['sourceStatus']=='observed' and ledger['historyComplete'] is False
    assert len(ledger['assets'][symbol]['events'])==1
    with f.begin() as s:
        r=s.scalar(select(JobCursor).where(JobCursor.job_name==module().JOB,JobCursor.cursor_key=='manifest'))
        r.cursor_value={**r.cursor_value,'baseVersion':'tampered'}
    bad=load_portfolio_dividend_ledger(f,(symbol,),date(2026,8,24),{symbol:'2026-01-01'})
    assert bad['sourceStatus']=='invalid_evidence' and not bad['assets'][symbol]['events']


def test_pending_batch_survives_new_base_day_then_refreshes_new_cutoff():
    f=source()
    with f.begin() as s:
        base=s.get(PublishedSnapshot,'screener')
        base.payload={**base.payload,'stocks':[*base.payload['stocks'],{'symbol':'600002.SH'}]}
    c=ObservedClient(f)
    assert refresh(f,c,budget=1)['status']=='pending'
    with f.begin() as s:
        base=s.get(PublishedSnapshot,'screener');base.version='next-day'
        base.as_of=base.as_of+timedelta(days=1)
    finished_old=refresh(f,c,budget=1,now=NOW+timedelta(days=3))
    assert finished_old['status']=='pending' and finished_old['requestCount']==1
    assert finished_old['code']=='DIVIDEND_QUERY_EXPIRED' and not finished_old['publishReady']
    with f() as s:
        manifest=module()._get(s,'manifest').cursor_value
        assert manifest['baseVersion']=='next-day' and manifest['asOf']=='2026-08-25'
    next_one=refresh(f,c,budget=1,now=NOW+timedelta(days=4))
    assert next_one['status']=='pending' and next_one['requestCount']==1
    next_two=refresh(f,c,budget=1,now=NOW+timedelta(days=4))
    assert next_two['status']=='completed' and next_two['requestCount']==1
    assert next_two['publishReady'] and len(c.calls)==4
    assert refresh(f,c,now=NOW+timedelta(days=4))['requestCount']==0


def test_reference_failure_prevents_portfolio_publication_but_reports_http(monkeypatch):
    from app.config import Settings
    import app.portfolio.reference_catalog as catalog
    import app.portfolio.reference_history as history
    import app.portfolio.marts.portfolio_runtime as publication
    from contextlib import contextmanager
    import app.shared.readonly_database as database
    import app.db.session as db
    f=source();engine=f.kw['bind']
    monkeypatch.setattr(engine,'dispose',lambda:None)
    monkeypatch.setattr(db,'create_engine_and_session',lambda url:(engine,f))
    monkeypatch.setattr(module(),'portfolio_routine_config',lambda:dict(enabled=True,referenceEnabled=True,dividendsEnabled=False,referenceIntervalDays=1))
    monkeypatch.setattr(catalog,'run_catalog',lambda *a,**kw:dict(status='completed',catalogReady=True,requestCount=0))
    monkeypatch.setattr(history,'run_history',lambda *a,**kw:dict(status='failed',requestCount=2,historyReady=False))
    monkeypatch.setattr(publication,'rebuild_portfolio',lambda *a,**kw:pytest.fail('failed reference must not publish'))
    r=module().run_portfolio_step(Settings(data_backend='database',database_url='sqlite://'),now=NOW,dry_run=False,publish=True)
    assert r['status']=='failed' and r['requestCount']==2 and not r['publishReady']


def test_only_portfolio_selects_no_other_sources_or_mutates_settings(monkeypatch):
    from backend.tests.jobs.test_routine_update import setup_steps,settings
    m,calls=setup_steps(monkeypatch)
    monkeypatch.setattr(m,'portfolio_routine_config',lambda:{'enabled':True})
    monkeypatch.setattr(m,'run_portfolio_step',lambda *a,**kw:dict(status='planned',requestCount=0))
    s=settings(); original=s.model_dump()
    r=m.run_routine_update(s,now=NOW,dry_run=True,only_portfolio=True,macro_rebuild=lambda s:None)
    assert not calls and s.model_dump()==original
    assert r['stocks']['status']=='not_selected' and r['macro']['status']=='not_selected'
    assert r['httpCounts']==dict(stocks=0,macro=0,portfolio=0,total=0)


def test_all_units_finished_before_manifest_crash_recovers_without_client():
    from app.portfolio.dividend_capture import capture_dividend_evidence
    f=source()
    with f.begin() as s:
        b=s.get(PublishedSnapshot,'screener');b.payload={**b.payload,'stocks':[*b.payload['stocks'],{'symbol':'600002.SH'}]}
    c=ObservedClient(f); first=refresh(f,c,budget=1)
    with f() as s:
        active=module()._get(s,'active').cursor_value
        plan=module()._get(s,active['planKey']).cursor_value
    capture_dividend_evidence(f,c,execute=True,config=config(20),job=module().JOB,
        control_key=f"capture:{first['identity']}",base_reader=lambda s:plan['target'])
    # 恢复入口读到全部完成，不创建HTTP客户端，仍须完成manifest。
    assert refresh(f,execute=False)['pendingCount']==0
    result=refresh(f)
    assert result['status']=='completed' and result['requestCount']==0


def test_core_change_during_capture_prevents_publish_until_next_increment():
    f=source()
    with f.begin() as s:
        b=s.get(PublishedSnapshot,'screener');a=b.payload['stocks'][0]['symbol']
        b.payload={**b.payload,'stocks':[*b.payload['stocks'],{'symbol':'600002.SH'}]}
    c=ObservedClient(f);refresh(f,c)
    with f.begin() as s:s.add(Dividend(ts_code=a,data={'revision':'A'}))
    class Mutating(ObservedClient):
        def call(self,*args):
            result=super().call(*args)
            with f.begin() as s:s.add(Dividend(ts_code='600002.SH',data={'revision':'B'}))
            return result
    r=refresh(f,Mutating(f))
    assert r['status']=='pending' and r['publishReady'] is False
    assert refresh(f,c)['requestCount']==1


def test_checks_use_each_unit_completion_instead_of_cycle_start():
    f=source()
    with f.begin() as s:
        b=s.get(PublishedSnapshot,'screener');b.payload={**b.payload,'stocks':[*b.payload['stocks'],{'symbol':'600002.SH'}]}
    c=ObservedClient(f);refresh(f,c,budget=1)
    refresh(f,c,budget=1,now=NOW+timedelta(days=4))
    result=refresh(f,c,now=NOW+timedelta(days=8))
    assert result['requestCount']==1


@pytest.mark.parametrize('status',['failed','reserved'])
def test_reference_new_target_cannot_bypass_old_failed_or_unknown_unit(monkeypatch,status):
    from backend.tests.test_reference_catalog import factory
    import app.portfolio.reference_history as m
    from app.ingestion.repositories import content_hash
    f=factory()
    old=dict(schemaVersion=1,baseVersion='old',asOf='2026-10-21',start='2026-10-20',
             targets=[{'target':'H00300','code':'H00300.CSI'}],fields=list(m.FIELDS),calendarHash='old')
    with f.begin() as s:
        s.add_all([JobCursor(job_name=m.JOB,cursor_key='contract:'+content_hash(old),cursor_value=old),
                   JobCursor(job_name=m.JOB,cursor_key=content_hash(old)+':H00300',cursor_value={'status':status})])
    current=old|dict(baseVersion='new',asOf='2026-10-22')
    monkeypatch.setattr(m,'history_plan',lambda s:(current,[dict(target='H00300',api='index_daily',params={'ts_code':'H00300.CSI'},fields=m.FIELDS)],('2026-10-22',)))
    class Forbidden:
        calls=0
        def set_response_observer(self,o):pass
        def call(self,*a):self.calls+=1;raise TimeoutError()
    client=Forbidden();r=m.capture_history(f,client,execute=True)
    assert r['status']=='blocked' and r['requestCount']==0 and client.calls==0


def test_reference_partial_finishes_frozen_window_then_prepares_new_day(monkeypatch):
    import json,base64,hashlib
    from datetime import date
    from app.db.models import TradeCalendar,DailyPrice
    from app.portfolio.reference_catalog import capture_catalog
    from app.connectors.tushare.contracts import TushareResponse
    import app.portfolio.reference_catalog as catalog
    import app.portfolio.reference_history as history
    f=source()
    with f.begin() as s:
        first=s.scalar(select(func.min(DailyPrice.trade_date)))
        day=first
        while day<=date(2026,8,25):
            if s.scalar(select(TradeCalendar).where(TradeCalendar.exchange=='SSE',TradeCalendar.cal_date==day)) is None:
                s.add(TradeCalendar(exchange='SSE',cal_date=day,is_open=day.weekday()<5))
            day+=timedelta(days=1)
    class Reference:
        def __init__(self):self.calls=[]
        def set_response_observer(self,observer):self.observer=observer
        def call(self,api,params,fields):
            self.calls.append((api,deepcopy(params)))
            if api=='index_basic':
                row=dict.fromkeys(fields)
                row.update(ts_code=params['symbol']+'.CSI',fullname='中证全收益'+params['symbol'],publisher='中证指数有限公司',market='CSI')
                rows=[row]
            else:
                with f() as s:
                    days=s.scalars(select(TradeCalendar.cal_date).where(TradeCalendar.exchange=='SSE',TradeCalendar.is_open.is_(True),
                        TradeCalendar.cal_date>=date.fromisoformat(params['start_date']),TradeCalendar.cal_date<=date.fromisoformat(params['end_date'])).order_by(TradeCalendar.cal_date)).all()
                rows=[dict(ts_code=params['ts_code'],trade_date=d.strftime('%Y%m%d'),close=100+i) for i,d in enumerate(days)]
            data=dict(fields=list(fields),items=[[r[k] for k in fields] for r in rows])
            body=json.dumps({'code':0,'data':data}).encode()
            self.observer(dict(statusCode=200,credentialsRedacted=False,bodyBase64=base64.b64encode(body).decode(),bodySha256=hashlib.sha256(body).hexdigest(),byteLength=len(body)))
            return TushareResponse.model_validate(data)
    c=Reference();assert capture_catalog(f,c,execute=True)['catalogReady']
    monkeypatch.setattr(catalog,'reference_config',lambda:dict(maxHttpPerRun=1,timeoutSeconds=20))
    one=history.capture_history(f,c,execute=True)
    assert one['status']=='pending' and one['requestCount']==1
    with f.begin() as s:
        base=s.get(PublishedSnapshot,'screener');base.version='new-day';base.as_of+=timedelta(days=1)
    resumed=history.capture_history(f,c,execute=True)
    assert resumed['code']=='REFERENCE_PREVIOUS_TARGET_FINISHED' and resumed['requestCount']==1
    assert history.capture_history(f,c,execute=True)['status']=='pending'
    assert history.capture_history(f,c,execute=True)['historyReady']
    assert history.capture_history(f,c,execute=True)['requestCount']==0
    assert len([v for v in c.calls if v[0]=='index_daily'])==4

    # 仅同日选股器版本变化，捕获和组合构建都应复用已验证的指数原件。
    with f() as s:
        contract, _, _ = history.history_plan(s)
    published = history.load_benchmarks(f, date.fromisoformat(contract['asOf']), contract['start'])
    assert len(published) == 2
    with f.begin() as s:
        s.get(PublishedSnapshot, 'screener').version = 'same-day-stock-update'
    reused = history.capture_history(f, c, execute=True)
    assert reused['historyReady'] and reused['requestCount'] == 0
    assert reused['identity'] == history.content_hash(contract)
    assert history.load_benchmarks(f, date.fromisoformat(contract['asOf']), contract['start']) == published
    # 窗口不同不能复用，两种路径都不能放宽原始响应哈希校验。
    shifted = (date.fromisoformat(contract['start']) + timedelta(days=1)).isoformat()
    assert not history.load_benchmarks(f, date.fromisoformat(contract['asOf']), shifted)
    with f.begin() as s:
        unit = history._cursor(s, history.JOB, f"{reused['identity']}:{history.TARGETS[0]}")
        raw = s.get(history.SourcePayload, unit.cursor_value['rawId'])
        raw.content_hash = 'tampered'
    with pytest.raises(ValueError, match='REFERENCE_EVIDENCE_INVALID'):
        history.load_benchmarks(f, date.fromisoformat(contract['asOf']), contract['start'])
    blocked = history.capture_history(f, c, execute=True)
    assert blocked['status'] == 'blocked' and blocked['requestCount'] == 0
