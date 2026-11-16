"""扩字段仅补改表，冻结来源引用仍校验原字节和完整分页。"""
from backend.tests.support.snapshots import set_snapshot_version
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import pytest
from sqlalchemy import select
from app.db.models import JobCursor,PublishedSnapshot,SourcePayload,IngestionBatch
from backend.tests.test_dividend_evidence_capture import source,db_state
from backend.tests.test_financial_supply import Client,module

FIELDS=('depr_fa_coga_dpba','amort_intang_assets','lt_amort_deferred_exp')


def expanded(monkeypatch):
    m=module();policy=deepcopy(m.config());old=deepcopy(policy)
    for field in FIELDS:
        old['tables']['cashflow']['fields']=[v for v in old['tables']['cashflow']['fields'] if v!=field]
    policy['tables']['cashflow']['fields']=list(dict.fromkeys([*policy['tables']['cashflow']['fields'],*FIELDS]))
    monkeypatch.setattr(m,'config',lambda:deepcopy(policy))
    return old,policy


def prepare(new_pg_factory, monkeypatch):
    m=module();f=source(new_pg_factory);client=Client();policy=deepcopy(m.config())
    policy['tables']['cashflow']['fields']=[v for v in policy['tables']['cashflow']['fields'] if v not in FIELDS]
    monkeypatch.setattr(m,'config',lambda:deepcopy(policy))
    first=m.capture(f,client,execute=True)
    assert first['status']=='completed'
    before=db_state(f);old,policy=expanded(monkeypatch)
    return m,f,client,first,before,policy


def test_only_changed_table_requests_new_originals_and_all_actual_lineage_remains(new_pg_factory, monkeypatch):
    m,f,c,first,before,policy=prepare(new_pg_factory, monkeypatch)
    preview=m.capture(f,execute=False)
    assert preview['completedCount']==2 and preview['unitCount']==3
    assert db_state(f)==before
    result=m.capture(f,c,execute=True,max_http=1)
    assert result['status']=='completed' and result['requestCount']==1
    assert len(c.calls)==4 and c.calls[-1][0]=='cashflow' and set(FIELDS)<=set(c.calls[-1][2])
    with f() as s:
        controls=s.scalars(select(JobCursor).where(JobCursor.cursor_key.like('contract:%')).order_by(JobCursor.id)).all()
        assert controls[1].cursor_value['reuseTables']=={'income':first['identity'],'balance':first['identity']}
        original=s.scalars(select(SourcePayload).order_by(SourcePayload.id)).all()
        assert len(original)==4
    points=m.load_points(f,datetime(2026,8,24).date(),('600519.SH',))
    assert len(points)==6
    cf=[p for p in points if p.source_table=='core.cashflow_statement']
    assert all(set(FIELDS)<=set(p.values) and p.values['_sourceId'].startswith('raw.source_payload:4:') for p in cf)
    for table in ('core.income_statement','core.balance_sheet'):
        assert all(not p.values['_sourceId'].startswith('raw.source_payload:4:') for p in points if p.source_table==table)
    saved=db_state(f);assert m.capture(f,c,execute=True)['requestCount']==0 and db_state(f)==saved
    assert len(c.calls)==4


@pytest.mark.parametrize('changed',['scope-date','signature','expired'])
def test_changed_scope_signature_or_expired_originals_are_never_reused(new_pg_factory, monkeypatch,changed):
    m,f,c,_,_,_=prepare(new_pg_factory, monkeypatch)
    kwargs={}
    if changed=='scope-date':
        with f.begin() as s:
            row=s.get(PublishedSnapshot,'screener');row.as_of+=timedelta(days=1);set_snapshot_version(s, row, 'new')
    elif changed=='signature':monkeypatch.setattr(m,'_core_signature',lambda *_:'new-visible-core')
    else:kwargs['now']=datetime.now(timezone.utc)+timedelta(days=8)
    preview=m.capture(f,execute=False,**kwargs)
    assert preview['completedCount']==0 and preview['unitCount']==3


def test_rebuilt_same_date_pointer_preserves_actual_origin_version_and_reuses_unchanged_facts(new_pg_factory, monkeypatch):
    m,f,c,first,_,_=prepare(new_pg_factory, monkeypatch)
    with f.begin() as s:
        old_version=s.get(PublishedSnapshot,'screener').version
        set_snapshot_version(s, s.get(PublishedSnapshot,'screener'), 'same-date-rebuilt')
    preview=m.capture(f,execute=False);assert preview['completedCount']==2
    assert m.capture(f,c,execute=True)['requestCount']==1
    points=m.load_points(f,datetime(2026,8,24).date(),('600519.SH',))
    for point in points:
        assert point.values['_sourceBaseVersion']==('same-date-rebuilt' if point.source_table=='core.cashflow_statement' else old_version)


def test_corrupt_prior_raw_never_causes_fallback_or_supplier_request(new_pg_factory, monkeypatch):
    m,f,c,_,_,_=prepare(new_pg_factory, monkeypatch)
    with f.begin() as s:s.get(SourcePayload,1).content_hash='invalid'
    before=db_state(f);result=m.capture(f,c,execute=True)
    assert result['status']=='blocked' and result['requestCount']==0 and len(c.calls)==3
    assert db_state(f)==before


def test_failed_changed_table_is_not_bypassed_by_another_configuration(new_pg_factory, monkeypatch):
    m,f,c,_,_,policy=prepare(new_pg_factory, monkeypatch)
    class Broken(Client):
        def call(self,*args):self.calls.append(args);raise TimeoutError('private')
    broken=Broken();result=m.capture(f,broken,execute=True,max_http=1)
    assert result['status'] in ('failed','blocked') and len(broken.calls)==1 and broken.calls[0][0]=='cashflow'
    policy['historyYears']+=1
    result=m.capture(f,broken,execute=True)
    assert result['status']=='blocked' and result['requestCount']==0 and len(broken.calls)==1


def test_unfinished_old_batch_is_resumed_before_fields_migration(new_pg_factory, monkeypatch):
    m=module();f=source(new_pg_factory);c=Client();policy=deepcopy(m.config())
    policy['tables']['cashflow']['fields']=[v for v in policy['tables']['cashflow']['fields'] if v not in FIELDS]
    monkeypatch.setattr(m,'config',lambda:deepcopy(policy))
    m.capture(f,c,execute=True,max_http=1)
    expanded(monkeypatch)
    result=m.capture(f,c,execute=True,max_http=1)
    assert result['completedCount']==2 and c.calls[-1][0]=='balancesheet'
    assert not set(FIELDS)<=set(c.calls[-1][2])


def test_historical_reference_cannot_cross_core_signature_even_with_rehashed_contract(new_pg_factory, monkeypatch):
    from app.ingestion.repositories import content_hash
    m,f,c,_,_,_=prepare(new_pg_factory, monkeypatch);assert m.capture(f,c,execute=True)['status']=='completed'
    with f.begin() as s:
        row=s.scalars(select(JobCursor).where(JobCursor.cursor_key.like('contract:%')).order_by(JobCursor.id.desc())).first()
        value=deepcopy(row.cursor_value);identity=content_hash(value);value['coreSignature']='different'
        new_hash=content_hash(value);row.cursor_key='contract:'+new_hash;row.cursor_value=value
        for unit in s.scalars(select(JobCursor).where(JobCursor.cursor_key.like(identity+':%'))):
            unit.cursor_key=new_hash+unit.cursor_key[len(identity):]
    with pytest.raises(ValueError,match='FINANCIAL_REUSE_INVALID'):
        m.load_points(f,datetime(2026,8,24).date(),('600519.SH',))


def test_newly_captured_contract_does_not_hide_older_visible_macro_evidence(new_pg_factory, monkeypatch):
    from backend.tests.macro.test_stock_history_bridge import prepared,inputs,bridge
    m=module();policy=deepcopy(m.config())
    policy['tables']['cashflow']['fields']=[v for v in policy['tables']['cashflow']['fields'] if v not in FIELDS]
    monkeypatch.setattr(m,'config',lambda:deepcopy(policy))
    f=prepared(new_pg_factory);before=bridge().load_stock_history_bridge(f,inputs(),datetime(2026,11,7).date(),enabled=True)
    assert len(before.financials)==6
    expanded(monkeypatch)
    assert m.capture(f,Client(),execute=True,now=datetime(2026,11,7,tzinfo=timezone.utc))['status']=='completed'
    result=bridge().load_stock_history_bridge(f,inputs(),datetime(2026,11,7).date(),enabled=True)
    assert result.financials==before.financials
    # 新期完整原件实际可见后，才切换；不能始终锁死在旧合同。
    later=bridge().load_stock_history_bridge(f,inputs(),datetime(2099,1,1).date(),enabled=True)
    assert any(r.source_key.startswith('raw.source_payload:4:') for r in later.financials)


def test_repeated_page_is_detected_even_when_actual_raw_ids_differ(new_pg_factory):
    import base64,hashlib,json
    from app.connectors.tushare.contracts import TushareResponse
    class Repeating(Client):
        def call(self,api,params,fields):
            self.calls.append((api,deepcopy(params),fields))
            row=dict.fromkeys(fields);row.update(ts_code=params['ts_code'],end_date='20241231',ann_date='20250301',
                f_ann_date='20250301',report_type='1',comp_type='1',update_flag='0')
            data=dict(fields=list(fields),items=[[row[k] for k in fields]],has_more=api=='income')
            body=json.dumps({'code':0,'data':data}).encode()
            self.callback(dict(statusCode=200,credentialsRedacted=False,bodyBase64=base64.b64encode(body).decode(),
                bodySha256=hashlib.sha256(body).hexdigest(),byteLength=len(body)))
            return TushareResponse.model_validate(data)
    f=source(new_pg_factory);c=Repeating();result=module().capture(f,c,execute=True,max_http=4)
    assert result['status']=='blocked' and result['code']=='FINANCIAL_PAGING_NO_PROGRESS'
    assert result['requestCount']==4 and len(c.calls)==4


def test_referenced_table_retains_its_own_refresh_deadline(new_pg_factory, monkeypatch):
    m,f,c,_,_,policy=prepare(new_pg_factory, monkeypatch);t0=datetime(2026,11,1,tzinfo=timezone.utc)
    with f.begin() as s:
        for raw in s.scalars(select(SourcePayload)):raw.captured_at=t0
        for batch in s.scalars(select(IngestionBatch).where(IngestionBatch.dataset==m.JOB)):batch.finished_at=t0
    assert m.capture(f,c,execute=True,now=t0+timedelta(days=6))['requestCount']==1
    with f.begin() as s:
        raw=s.get(SourcePayload,4);raw.captured_at=t0+timedelta(days=6)
        s.get(IngestionBatch,raw.batch_id).finished_at=raw.captured_at
    # 相同合同也不能因cashflow较新而永远不刷新income/balance。
    same=m.capture(f,execute=False,now=t0+timedelta(days=8))
    assert same['completedCount']==1
    policy['tables']['cashflow']['fields'].append('review_extra_amount')
    assert m.capture(f,execute=False,now=t0+timedelta(days=8))['completedCount']==0


def test_complete_referenced_contract_failure_is_not_hidden_by_new_table(new_pg_factory, monkeypatch):
    m,f,c,first,_,_=prepare(new_pg_factory, monkeypatch);assert m.capture(f,c,execute=True)['status']=='completed'
    with f.begin() as s:
        row=s.scalar(select(JobCursor).where(JobCursor.cursor_key==first['identity']+':cashflow:600519.SH'))
        row.cursor_value=row.cursor_value|{'status':'failed'}
    from backend.tests.macro.test_stock_history_bridge import bridge,inputs
    with pytest.raises(ValueError):bridge().load_stock_history_bridge(f,inputs(),datetime(2099,1,1).date(),enabled=True)
