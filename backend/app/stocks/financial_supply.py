"""批量财务报表补充原件；不改旧证券合同或Core事实，不抓公司网页。"""
from copy import deepcopy
from datetime import date,datetime,timezone,timedelta
from decimal import Decimal,InvalidOperation
import json
import base64
from sqlalchemy import select,text
from app.config_paths import CONFIG_ROOT
from app.db.models import PublishedSnapshot,JobCursor,SourcePayload,IngestionBatch,IncomeStatement,BalanceSheet,CashflowStatement
from app.stocks.derived.inputs import FinancialPoint
from app.ingestion.repositories import content_hash
from app.jobs.locks import job_lock_for_engine
from app.portfolio.api.dividends import original_dividend_rows
from app.portfolio.api.reference import capture_catalog
from app.connectors.tushare.contracts import request_hash

CONFIG_FILE=CONFIG_ROOT / "stocks/financial-indicators.json"
JOB='financial-statement-evidence'
KINDS={'income':('income','core.income_statement','利润表'),'balance':('balancesheet','core.balance_sheet','资产负债表'),'cashflow':('cashflow','core.cashflow_statement','现金流量表')}
META={'ts_code','ann_date','f_ann_date','end_date','report_type','comp_type','update_flag'}


def config():
    if not CONFIG_FILE.is_file():return dict(enabled=False)
    value=json.loads(CONFIG_FILE.read_text(encoding='utf-8'))['dataSupply']
    if type(value['enabled']) is not bool:raise ValueError('FINANCIAL_CONFIG_INVALID')
    for key,maximum in [('maxHttpPerRun',10000),('historyYears',30),('refreshDays',365),('timeoutSeconds',120)]:
        if type(value[key]) is not int or not 1<=value[key]<=maximum:raise ValueError('FINANCIAL_CONFIG_INVALID')
    for kind,(api,_,_) in KINDS.items():
        row=value['tables'][kind]
        if row['api']!=api or not META<=set(row['fields']) or len(set(row['fields']))!=len(row['fields']):raise ValueError('FINANCIAL_FIELDS_INVALID')
    return value


def forecast_policy():
    value = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("forecastDetail", {"enabled": False})
    if type(value.get("enabled")) is not bool:
        raise ValueError("FORECAST_CONFIG_INVALID")
    if value["enabled"] and any(type(value.get(k)) is not int or value[k] < 1 for k in ("maxChecks", "retryDays", "backoffMinutes")):
        raise ValueError("FORECAST_CONFIG_INVALID")
    return value


def _base(session):
    base=session.get(PublishedSnapshot,'screener')
    if base is None:raise ValueError('FINANCIAL_BASE_REQUIRED')
    symbols=sorted(r['symbol'] for r in base.payload['stocks'])
    if not symbols or len(set(symbols))!=len(symbols):raise ValueError('FINANCIAL_SCOPE_INVALID')
    return dict(baseVersion=base.version,asOf=base.as_of.date().isoformat(),symbols=symbols)


def _cursor(s,key):return s.scalar(select(JobCursor).where(JobCursor.job_name==JOB,JobCursor.cursor_key==key))


def _core_signature(s,symbols,as_of):
    rows=[]
    for model in (IncomeStatement,BalanceSheet,CashflowStatement):
        for row in s.scalars(select(model).where(model.ts_code.in_(symbols)).order_by(model.id)):
            ann=row.f_ann_date or row.ann_date
            if ann and ann<=as_of and row.end_date<=as_of:
                rows.append((model.__tablename__,row.id,str(ann),row.report_type,row.update_flag,row.data))
    return content_hash(rows)


def _requests(contract):
    start=f"{date.fromisoformat(contract['asOf']).year-contract['policy']['historyYears']}0101"
    return [dict(target=f'{kind}:{symbol}',api=policy['api'],fields=policy['fields'],params={'ts_code':symbol,'start_date':start,'end_date':contract['asOf'].replace('-',''),'report_type':'1'})
        for symbol in contract['symbols'] for kind,policy in contract['policy']['tables'].items()]


def _validate(response,spec):
    for row in response.rows():
        if row.get('ts_code')!=spec['params']['ts_code']:raise ValueError('FINANCIAL_SYMBOL_MISMATCH')
        for key in ('end_date','ann_date','f_ann_date'):
            if row.get(key) not in (None,''):date.fromisoformat(str(row[key]))
        if row.get('end_date') in (None,''):raise ValueError('FINANCIAL_PERIOD_MISSING')
        for key,value in row.items():
            if key in META or value in (None,''):continue
            if isinstance(value,bool) or not Decimal(str(value)).is_finite():raise ValueError('FINANCIAL_NONFINITE')


def _source_contract(s,contract,spec):
    """按表引用实际旧合同，不复制Raw、游标或伪装成新请求。"""
    kind=spec['target'].split(':')[0];seen=set()
    while 'reuseTables' in contract:
        refs=contract['reuseTables']
        if type(refs) is not dict or not set(refs)<=set(KINDS):raise ValueError('FINANCIAL_REUSE_INVALID')
        if kind not in refs:break
        identity=refs[kind]
        if type(identity) is not str or len(identity)!=64 or identity in seen or len(seen)>=8:raise ValueError('FINANCIAL_REUSE_INVALID')
        seen.add(identity)
        row=_cursor(s,'contract:'+identity)
        if row is None or content_hash(row.cursor_value)!=identity:raise ValueError('FINANCIAL_REUSE_INVALID')
        old=row.cursor_value
        _validate_reference(s,old)
        anchor=_cursor(s,'core-anchor:'+identity)
        signature=old.get('coreSignature') or (anchor.cursor_value.get('signature') if anchor else None)
        if (old['symbols']!=contract['symbols'] or old['asOf']!=contract['asOf']
            or signature is None or signature!=contract.get('coreSignature')
            or old['policy']['tables'][kind]!=contract['policy']['tables'][kind]
            or old['policy']['historyYears']!=contract['policy']['historyYears']):raise ValueError('FINANCIAL_REUSE_INVALID')
        contract=old
    root=spec['target'].split(':offset:')[0]
    expected=next((r for r in _requests(contract) if r['target']==root),None)
    plain=deepcopy(spec);plain['target']=root
    plain['params']={k:v for k,v in spec['params'].items() if k not in ('offset','limit')}
    if expected!=plain:raise ValueError('FINANCIAL_REUSE_INVALID')
    return contract


def _validate_reference(s,contract):
    identity=content_hash(contract);transaction=s.get_transaction()
    cache=s.info.get('financial-reference-validation')
    if cache is None or cache['transaction'] is not transaction:
        cache=dict(transaction=transaction,valid=set(),visiting=set());s.info['financial-reference-validation']=cache
    if identity in cache['valid']:return
    if identity in cache['visiting']:raise ValueError('FINANCIAL_REUSE_INVALID')
    cache['visiting'].add(identity)
    try:
        for spec in _expanded_requests(s,contract):
            origin=_source_contract(s,contract,spec)
            unit=_cursor(s,f"{content_hash(origin)}:{spec['target']}")
            if unit is not None and unit.cursor_value.get('status')!='completed':raise ValueError('FINANCIAL_UNRESOLVED_PRIOR_UNIT')
            if _saved(s,contract,spec) is None:raise ValueError('FINANCIAL_REUSE_INVALID')
        cache['valid'].add(identity)
    finally:cache['visiting'].discard(identity)


def _table_fresh(s,contract,kind,now,days):
    stamps=[]
    for spec in _expanded_requests(s,contract):
        if spec['target'].split(':')[0]!=kind:continue
        saved=_saved(s,contract,spec)
        if saved is None:return False
        raw,_=saved;batch=s.get(IngestionBatch,raw.batch_id)
        if batch.finished_at is None:return False
        for value in (raw.captured_at,batch.finished_at):
            stamps.append(value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc))
    return bool(stamps) and max(stamps)<=now and now-max(stamps)<timedelta(days=days)


def _saved(s,contract,spec):
    contract=_source_contract(s,contract,spec)
    unit=_cursor(s,f"{content_hash(contract)}:{spec['target']}")
    if unit is None or unit.cursor_value.get('status')!='completed':return None
    unit=unit.cursor_value;raw=s.get(SourcePayload,unit.get('rawId'));batch=s.get(IngestionBatch,unit.get('batchId'))
    if raw is None or batch is None or raw.batch_id!=batch.id or batch.dataset!=JOB or batch.status!='completed' or batch.source!='tushare' or batch.request_count!=1 or batch.rejected_count!=0 or raw.api_name!=spec['api'] or raw.request_hash!=request_hash(spec['api'],spec['params'],spec['fields']) or raw.content_hash!=unit.get('responseContentHash') or content_hash(raw.payload)!=raw.content_hash:raise ValueError('FINANCIAL_EVIDENCE_INVALID')
    rows=original_dividend_rows(raw,spec['fields'],allow_more=True)
    if len(rows)!=unit.get('rowCount') or len(rows)!=batch.row_count:raise ValueError('FINANCIAL_ROW_COUNT_INVALID')
    return raw,rows


def _expanded_requests(s,contract,*,targets=None,saved_pages=None):
    requests=[spec for spec in _requests(contract) if targets is None or spec['target'] in targets]
    index=0;seen={}
    while index<len(requests):
        spec=requests[index];index+=1
        saved=_saved(s,contract,spec)
        if saved is None:continue
        raw,rows=saved
        if saved_pages is not None:saved_pages[spec['target']]=saved
        data=json.loads(base64.b64decode(raw.payload['_http']['bodyBase64']))['data']
        root=spec['target'].split(':offset:')[0]
        fingerprint=content_hash([{k:v for k,v in row.items() if k!='sourceId'} for row in rows])
        seen.setdefault(root,set())
        if rows and fingerprint in seen[root]:raise ValueError('FINANCIAL_PAGING_NO_PROGRESS')
        seen[root].add(fingerprint)
        if data.get('has_more') is True:
            if not rows or len(seen[root])>=100:raise ValueError('FINANCIAL_PAGING_LIMIT')
            offset=spec['params'].get('offset',0)+len(rows)
            requests.append(spec|dict(target=f'{root}:offset:{offset}',params=spec['params']|dict(offset=offset,limit=100)))
    return requests


def metric_card_originals(session, symbols, as_of, *, report_fields=None):
    """合并历批完整原件的可见版本；相同八卡事实沿用首次可核验的逐行出处。"""
    from types import SimpleNamespace
    from app.ingestion.normalizers import parse_date
    from app.stocks.derived.inputs import CARD_REPORT_FIELDS
    fields = CARD_REPORT_FIELDS if report_fields is None else report_fields
    requested = {f"{kind}:{code}" for code in symbols for kind in fields if kind in KINDS}
    seen = set()
    query = select(JobCursor).where(JobCursor.job_name == JOB, JobCursor.cursor_key.startswith("contract:"))
    for control in session.scalars(query.order_by(JobCursor.id)):
        contract = control.cursor_value
        if control.cursor_key != "contract:" + content_hash(contract):
            raise ValueError("FINANCIAL_CONTRACT_INVALID")
        targets = {target for target in requested if target.split(":")[1] in contract["symbols"]}
        if not targets:
            continue
        saved = {}
        specs = _expanded_requests(session, contract, targets=targets, saved_pages=saved)
        for target in sorted(targets):
            pages = [spec["target"] for spec in specs if spec["target"].split(":offset:")[0] == target]
            if not pages or any(page not in saved for page in pages):
                continue
            kind, code = target.split(":")
            for page in pages:
                for item in saved[page][1]:
                    announced = parse_date(item.get("ann_date"))
                    revised = parse_date(item.get("f_ann_date"))
                    period = parse_date(item["end_date"])
                    if item.get("ts_code") != code:
                        raise ValueError("FINANCIAL_SYMBOL_MISMATCH")
                    if not (revised or announced) or (revised or announced) > as_of or period > as_of:
                        continue
                    # 合同未请求的字段与供应商明确报空有不同含义。
                    values = tuple((k, Decimal(str(item[k])) if item[k] not in (None, "") else None)
                                   for k in fields[kind] if k in item)
                    identity = (target, period, announced, revised, item.get("report_type"),
                                item.get("comp_type"), item.get("update_flag"), values)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    yield kind, SimpleNamespace(ts_code=code, end_date=period, ann_date=announced,
                        f_ann_date=revised, report_type=item.get("report_type"), comp_type=item.get("comp_type"),
                        update_flag=item.get("update_flag"), source_id=item["sourceId"], data=item)


def _reusable_tables(s,base,policy,signature,now,complete_contracts):
    refs={}
    for old,stamps,known_signature in reversed(complete_contracts):
        if (old['symbols']!=base['symbols'] or old['asOf']!=base['asOf'] or known_signature!=signature
            or not stamps or now-max(stamps)>=timedelta(days=policy['refreshDays'])
            or old['policy']['historyYears']!=policy['historyYears']):continue
        for kind in KINDS:
            if kind in refs or old['policy']['tables'][kind]!=policy['tables'][kind]:continue
            spec=next(r for r in _requests(old) if r['target'].split(':')[0]==kind)
            origin=_source_contract(s,old,spec)
            if not _table_fresh(s,origin,kind,now,policy['refreshDays']):continue
            refs[kind]=content_hash(origin)
    return refs


def capture(factory,client=None,*,execute=False,max_http=None,now=None):
    policy=config();now=now or datetime.now(timezone.utc)
    lock=job_lock_for_engine(factory.kw['bind']);key=JOB+':targets'
    acquired=execute and lock.try_acquire(key)
    spent=0
    result=dict(status='blocked',requestCount=0)
    if execute and not acquired:return dict(status='locked',requestCount=0)
    try:
        with factory() as s:
            if factory.kw['bind'].dialect.name=='postgresql':s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            base=_base(s);pending=None;reusable=None;complete_contracts=[]
            signature=_core_signature(s,base['symbols'],date.fromisoformat(base['asOf']))
            frozen_policy={k:v for k,v in policy.items() if k not in ('enabled','maxHttpPerRun','timeoutSeconds')}
            for row in s.scalars(select(JobCursor).where(JobCursor.job_name==JOB,JobCursor.cursor_key.like('contract:%')).order_by(JobCursor.id)):
                old=row.cursor_value
                if row.cursor_key!='contract:'+content_hash(old):raise ValueError('FINANCIAL_CONTRACT_INVALID')
                complete=True
                stamps=[]
                for spec in _expanded_requests(s,old):
                    unit=_cursor(s,f"{content_hash(old)}:{spec['target']}")
                    if unit is not None and unit.cursor_value.get('status')!='completed':raise ValueError('FINANCIAL_UNRESOLVED_PRIOR_UNIT')
                    saved=_saved(s,old,spec)
                    if saved is None:complete=False
                    else:
                        captured=saved[0].captured_at
                        stamps.append(captured.replace(tzinfo=timezone.utc) if captured.tzinfo is None else captured.astimezone(timezone.utc))
                if not complete and pending is None:pending=old
                anchor=_cursor(s,'core-anchor:'+content_hash(old))
                known_signature=old.get('coreSignature') or (anchor.cursor_value.get('signature') if anchor is not None else None)
                if complete:complete_contracts.append((old,stamps,known_signature))
                if complete and stamps and old['symbols']==base['symbols'] and old['asOf']<=base['asOf'] and old['policy']==frozen_policy and all(_table_fresh(s,old,k,now,policy['refreshDays']) for k in KINDS) and (known_signature==signature or (known_signature is None and old['baseVersion']==base['baseVersion'])):
                    reusable=old
            # 周期只选择新批次；旧未完优先，不能借新周期/配置绕过失败。
            contract=pending or reusable or (base|dict(policy=frozen_policy,coreSignature=signature,cycle=now.date().toordinal()//policy['refreshDays']))
            if pending is None and reusable is None:
                refs=_reusable_tables(s,base,policy,signature,now,complete_contracts)
                if refs:contract=contract|dict(reuseTables=refs)
            if contract['symbols']!=base['symbols']:raise ValueError('FINANCIAL_FROZEN_SCOPE_CHANGED')
            requests=_expanded_requests(s,contract)
        budget=policy['maxHttpPerRun'] if max_http is None else max_http
        if type(budget) is not int or budget<1:raise ValueError('FINANCIAL_BUDGET_INVALID')
        while True:
            active=[spec for spec in requests if spec['target'].split(':')[0] not in contract.get('reuseTables',{})]
            result=capture_catalog(factory,client,execute=execute,_job=JOB,_requests=active,_contract=contract,
                _control_key='contract:'+content_hash(contract),_config={'maxHttpPerRun':budget-spent},_validator=_validate,_stop_on_error=True)
            spent+=result['requestCount']
            result['requestCount']=spent
            result.pop('catalogReady',None)
            with factory() as s:
                requests=_expanded_requests(s,contract)
                completed=sum(_saved(s,contract,spec) is not None for spec in requests)
            if not execute or result['failedCount'] or completed==len(requests) or spent>=budget:break
            if not result['requestCount']:break
        result.update(identity=content_hash(contract),unitCount=len(requests),completedCount=completed,
            baseVersion=contract['baseVersion'],asOf=contract['asOf'],currentTarget=contract['baseVersion']==base['baseVersion'],
            compatibleTarget=contract['symbols']==base['symbols'] and contract['asOf']<=base['asOf'])
        if execute and not result['failedCount']:
            result['status']='completed' if completed==len(requests) else 'pending'
            if result['status']=='completed' and 'coreSignature' not in contract and contract['baseVersion']==base['baseVersion']:
                with factory.begin() as s:
                    anchor_key='core-anchor:'+content_hash(contract)
                    if _cursor(s,anchor_key) is None:s.add(JobCursor(job_name=JOB,cursor_key=anchor_key,cursor_value={'signature':signature}))
        return result
    except (ValueError,KeyError,TypeError,InvalidOperation) as exc:
        return result|dict(status='blocked',requestCount=spent,code=str(exc) if isinstance(exc,ValueError) and str(exc).startswith('FINANCIAL_') else 'FINANCIAL_EVIDENCE_INVALID')
    finally:
        if acquired:lock.release(key)


def load_points(factory,as_of,symbols,*,contract_identity=None,allow_new_price_day=False):
    with factory() as s:
        if s.get(PublishedSnapshot,'screener') is None:return ()
        contracts=list(s.scalars(select(JobCursor).where(JobCursor.job_name==JOB,JobCursor.cursor_key.like('contract:%')).order_by(JobCursor.id.desc())))
        # 未运行过本补采组时，普通/市场构建继续使用原Core链路。
        # 市场独立快照只需日期元数据，不应被附加要求具有完整选股器股票行。
        if not contracts:return ()
        base=_base(s)
        if base['asOf']!=as_of.isoformat() and not (allow_new_price_day and base['asOf']<as_of.isoformat()):return ()
        if set(symbols)!=set(base['symbols']):return ()
        matching=[]
        for control in contracts:
            candidate=control.cursor_value
            if candidate.get('symbols')!=base['symbols'] or candidate.get('asOf','9999')>as_of.isoformat():continue
            if control.cursor_key!='contract:'+content_hash(candidate):raise ValueError('FINANCIAL_CONTRACT_INVALID')
            matching.append(candidate)
        if contract_identity is not None:
            matching=[v for v in matching if content_hash(v)==contract_identity]
            if len(matching)!=1:raise ValueError('FINANCIAL_CONTRACT_INVALID')
        if not matching:return ()
        selected=None
        fallback=allow_new_price_day and contract_identity is None
        for candidate in matching:
            pages={}
            specs=_expanded_requests(s,candidate,saved_pages=pages)
            missing=[spec for spec in specs if spec['target'] not in pages]
            for spec in missing:
                unit=_cursor(s,f"{content_hash(candidate)}:{spec['target']}")
                if unit is None:continue
                status=unit.cursor_value.get('status')
                # 页面清单读取后，另一个事务可能刚刚提交了该单元，下次构建再用完整批次。
                if status=='completed':continue
                if not fallback or status not in ('reserved','failed'):
                    raise ValueError('FINANCIAL_UNRESOLVED_PRIOR_UNIT')
            # 正常股票构建可以用上个完整批次。审计和独立财报发布仍检查未解决单元。
            if not missing and selected is None:
                selected=(candidate,specs,pages)
                if fallback:break
        if selected is None:raise ValueError('FINANCIAL_BATCH_INCOMPLETE')
        contract,specs,pages=selected
        points=[]
        for spec in specs:
            saved=pages[spec['target']]
            raw,rows=saved;kind=spec['target'].split(':')[0]
            origin=_source_contract(s,contract,spec)
            for index,row in enumerate(rows,1):
                end=date.fromisoformat(str(row['end_date']));ann=row.get('f_ann_date') or row.get('ann_date')
                ann=date.fromisoformat(str(ann)) if ann else None
                if ann is not None and (ann.isoformat()>contract['asOf'] or end.isoformat()>contract['asOf']):continue
                points.append(FinancialPoint(end,ann,row.get('report_type'),row|{'_sourceId':f'raw.source_payload:{raw.id}:row:{index}','_capturedAt':raw.captured_at.isoformat(),'_sourceAsOf':origin['asOf'],'_sourceBaseVersion':origin['baseVersion']},spec['params']['ts_code'],KINDS[kind][1],f'raw:{raw.id}:{index}'))
        return tuple(points)


def _number(value):
    if value is None or value=='' or isinstance(value,bool):return None
    try:
        number=Decimal(str(value));result=float(number)
        return result if number.is_finite() and abs(result)!=float('inf') else None
    except (InvalidOperation,ValueError,OverflowError):return None


def statement_tables(points,as_of):
    tables=[]
    for kind,(_,source,label) in KINDS.items():
        selected=[p for p in points if p.source_table==source and p.report_type=='1' and p.disclosed_by(as_of) and p.end_date<=as_of]
        periods=sorted({p.end_date for p in selected});fields=sorted({k for p in selected for k in p.values if k not in META and k!='sourceId' and not k.startswith('_')})
        chosen=[];metadata=[]
        for period in periods:
            rows=[p for p in selected if p.end_date==period];latest=max(p.visible_at for p in rows);rows=[p for p in rows if p.visible_at==latest]
            flag=max(str(p.values.get('update_flag') or '') for p in rows);rows=[p for p in rows if str(p.values.get('update_flag') or '')==flag]
            announced = max(p.announcement_date or date.min for p in rows)
            rows = [p for p in rows if (p.announcement_date or date.min) == announced]
            values={k:{_number(p.values[k]) for p in rows if k in p.values} for k in fields}
            conflict_fields=[key for key,observed in values.items() if len(observed)>1]
            chosen.append({k:next(iter(v)) if len(v)==1 else None for k,v in values.items()})
            metadata.append(dict(period=period.isoformat(),announcementDate=latest.isoformat(),reportType='1',
                updateFlag=flag, originalAnnouncementDate=announced.isoformat() if announced != date.min else None,
                conflict=bool(conflict_fields),conflictFields=conflict_fields,
                sourceIds=sorted({str(p.values.get('_sourceId') or p.source_record_key) for p in rows}),capturedAt=sorted({str(p.values.get('_capturedAt') or '') for p in rows})))
            if kind=='balance':
                types={str(p.values.get('comp_type') or '') for p in rows}
                if len(types)==1 and '' not in types:metadata[-1]['companyType']=types.pop()
            if kind=='cashflow':
                metadata[-1]['requestedFields']=[key for key,value in values.items() if value]
        if kind=='income':
            for row in chosen:
                row['gross_profit']=row['revenue']-row['oper_cost'] if row.get('revenue') is not None and row.get('oper_cost') is not None else None
            if 'gross_profit' not in fields:fields.append('gross_profit')
        if kind=='cashflow':
            for row in chosen:
                row['free_cash_flow']=row['n_cashflow_act']-row['c_pay_acq_const_fiolta'] if row.get('n_cashflow_act') is not None and row.get('c_pay_acq_const_fiolta') is not None else None
            if 'free_cash_flow' not in fields:fields.append('free_cash_flow')
        tables.append(dict(kind=kind,label=label,periods=[d.isoformat() for d in periods],amountUnit='yuan',reportMetadata=metadata,
            evidenceAsOf=min((p.values['_sourceAsOf'] for p in selected if '_sourceAsOf' in p.values),default=''),
            rows=[dict(label=k,values=[r.get(k) for r in chosen],yoy=[],qoq=[]) for k in fields]))
    return tables
