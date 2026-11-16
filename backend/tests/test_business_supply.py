"""主营证据：仅临时 PostgreSQL和合成HTTP，不使用用户配置/网络。"""
import base64,json
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from hashlib import sha256
import pytest
from sqlalchemy import select, func
from app.db.models import DataAcquisitionState,JobCursor,IngestionBatch,SourcePayload,PublishedSnapshot
from app.connectors.tushare.contracts import TushareResponse

NOW=datetime(2026,11,6,tzinfo=timezone.utc)

def module():
    from app.stocks import business_supply
    return business_supply

@pytest.fixture
def factory(pg_factory, monkeypatch,tmp_path):
    m=module();path=tmp_path/'financial.json'
    path.write_text(json.dumps({'businessSupply':dict(enabled=True,maxHttpPerRun=20,timeoutSeconds=20,historyYears=10,refreshDays=7)}))
    monkeypatch.setattr(m,'CONFIG_FILE',path)
    f = pg_factory
    with f.begin() as s:s.add(PublishedSnapshot(product='screener',version='base',as_of=NOW,payload={'stocks':[{'symbol':'600519.SH'}]}))
    yield f

class Client:
    def __init__(self,*,split=False,fail=False,empty=False,absent_flag=False,wrong=False,cap=False,too_many=False):
        self.calls=[];self.split=split;self.fail=fail;self.empty=empty;self.absent_flag=absent_flag;self.wrong=wrong;self.cap=cap;self.too_many=too_many
    def set_response_observer(self,observer):self.observer=observer
    def call(self,api,params,fields):
        self.calls.append((api,deepcopy(params),tuple(fields)))
        if self.fail:raise TimeoutError('失败不重试')
        end=params['end_date'];start=params['start_date']
        periods=[f'{year}{suffix}' for year in range(int(start[:4]),int(end[:4])+1) for suffix in ('0331','0630','0930','1231') if start<=f'{year}{suffix}'<=end]
        period='20251231' if start<='20251231'<=end else periods[-1] if periods else end
        items=[] if self.empty else [[params['ts_code'] if not self.wrong else '000001.SZ',period,'项目A',100,20,80,'CNY','0']]
        more=self.split and (int(end[:4])-int(start[:4]))>=5
        if more:items*=100
        if self.cap:items=[[params['ts_code'],'20251231',f'项目{i}',100,20,80,'CNY','0'] for i in range(100)] if start<='20251231'<=end else [];more=False
        if self.too_many:items*=8001
        data=dict(fields=list(fields),items=items)
        if not self.absent_flag:data['has_more']=more
        body=json.dumps(dict(code=0,data=data)).encode()
        self.observer(dict(statusCode=200,credentialsRedacted=False,bodyBase64=base64.b64encode(body).decode(),bodySha256=sha256(body).hexdigest(),byteLength=len(body)))
        return TushareResponse.model_validate(data)

def test_preview_is_readonly_and_no_old_fields_or_guessed_offset(factory):
    m=module();r=m.capture(factory,now=NOW)
    assert r['status']=='planned' and r['requestCount']==0 and r['unitCount']==2
    with factory() as s:assert s.scalar(select(func.count()).select_from(JobCursor))==0
    c=Client();assert m.capture(factory,c,execute=True,now=NOW)['status']=='completed'
    assert {p['type'] for _,p,_ in c.calls}=={'P','D'}
    assert all('bz_sales' in f and 'revenue' not in f and 'offset' not in p for _,p,f in c.calls)

def test_budget_resume_cross_day_and_same_target_no_http(factory):
    m=module();c=Client()
    a=m.capture(factory,c,execute=True,now=NOW,max_http=1)
    assert a['status']=='pending' and a['requestCount']==1
    b=m.capture(factory,c,execute=True,now=NOW+timedelta(days=1))
    assert b['status']=='completed' and b['requestCount']==1 and b['identity']==a['identity']
    assert m.capture(factory,c,execute=True,now=NOW+timedelta(days=2))['requestCount']==0

def test_saturated_ranges_split_and_only_leaf_rows_are_consumed(factory):
    m=module();c=Client(split=True)
    r=m.capture(factory,c,execute=True,now=NOW)
    assert r['status']=='completed' and r['requestCount']>2
    assert all('offset' not in p for _,p,_ in c.calls)
    evidence=m.load_business_evidence(factory,['600519.SH'],NOW.date())['600519.SH']
    assert len(evidence['rows'])==r['leafCount']
    assert len(evidence['rows'])<100

def test_failed_reservation_blocks_reentry_without_retry_or_new_cycle(factory):
    m=module();c=Client(fail=True)
    assert m.capture(factory,c,execute=True,now=NOW)['status']=='blocked'
    again=m.capture(factory,Client(),execute=True,now=NOW+timedelta(days=10))
    assert again['status']=='blocked' and again['requestCount']==0
    with factory() as s:assert s.scalar(select(func.sum(IngestionBatch.request_count)))==1

@pytest.mark.parametrize('damage',['bytes','request','batch','contract','scope'])
def test_bad_evidence_and_pending_scope_change_block_before_http(factory,damage):
    m=module();c=Client()
    m.capture(factory,c,execute=True,now=NOW,max_http=1)
    with factory.begin() as s:
        raw=s.scalar(select(SourcePayload))
        if damage=='bytes':raw.payload=raw.payload|{'_http':raw.payload['_http']|{'bodyBase64':'bad'}}
        elif damage=='request':raw.request_hash='wrong'
        elif damage=='batch':s.get(IngestionBatch,raw.batch_id).request_count=0
        elif damage=='contract':
            control=s.scalar(select(JobCursor).where(JobCursor.cursor_key.like('contract:%')))
            control.cursor_value=control.cursor_value|{'types':['P']}
        else:s.get(PublishedSnapshot,'screener').payload={'stocks':[{'symbol':'000001.SZ'}]}
    before=len(c.calls)
    assert m.capture(factory,c,execute=True,now=NOW)['status']=='blocked'
    assert len(c.calls)==before

def test_empty_response_is_observed_missing_not_zero_and_missing_terminal_flag_not_complete(factory):
    m=module();c=Client(empty=True,absent_flag=True)
    assert m.capture(factory,c,execute=True,now=NOW)['status']=='completed'
    item=m.load_business_evidence(factory,['600519.SH'],NOW.date())['600519.SH']
    assert item['rows']==[] and item['queryComplete'] is False

def test_wrong_security_is_preserved_failure_not_accepted(factory):
    m=module();c=Client(wrong=True)
    assert m.capture(factory,c,execute=True,now=NOW)['status']=='blocked'
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(SourcePayload))==1

def test_refresh_reuses_across_same_day_research_version_but_source_window_changes(factory):
    m=module();c=Client();first=m.capture(factory,c,execute=True,now=NOW)
    with factory.begin() as s:s.get(PublishedSnapshot,'screener').version='research-only'
    assert m.capture(factory,c,execute=True,now=NOW+timedelta(days=1))['requestCount']==0
    renewed=m.capture(factory,c,execute=True,now=NOW+timedelta(days=8))
    assert renewed['status']=='completed' and renewed['requestCount']==2 and renewed['identity']!=first['identity']


def test_one_report_still_at_row_cap_stays_blocked_even_if_flag_claims_false(factory):
    class Saturated(Client):
        def call(self,api,params,fields):
            self.calls.append((api,deepcopy(params),tuple(fields)))
            rows=[[params['ts_code'],'20251231',f'项目{i}',100,20,80,'CNY','0'] for i in range(100)] if params['start_date']<='20251231'<=params['end_date'] else []
            data=dict(fields=list(fields),items=rows,has_more=False)
            body=json.dumps(dict(code=0,data=data)).encode()
            self.observer(dict(statusCode=200,credentialsRedacted=False,bodyBase64=base64.b64encode(body).decode(),bodySha256=sha256(body).hexdigest(),byteLength=len(body)))
            return TushareResponse.model_validate(data)
    m=module();c=Saturated()
    for _ in range(5):
        r=m.capture(factory,c,execute=True,now=NOW)
        if r['status']=='blocked':break
        assert r['status']=='pending'
    assert r['status']=='blocked' and r['code']=='BUSINESS_ROW_CAP_EXCEEDED'
    count=len(c.calls)
    assert m.capture(factory,c,execute=True,now=NOW+timedelta(days=9))['requestCount']==0
    assert len(c.calls)==count


def test_new_failed_refresh_preserves_previous_completed_source(factory):
    m=module();c=Client();m.capture(factory,c,execute=True,now=NOW)
    before=m.load_business_evidence(factory,['600519.SH'],NOW.date())
    assert m.capture(factory,Client(fail=True),execute=True,now=NOW+timedelta(days=8))['status']=='blocked'
    assert m.load_business_evidence(factory,['600519.SH'],NOW.date())==before


def test_row_cap_blocked_refresh_preserves_old_completed_reader(factory):
    m=module();m.capture(factory,Client(),execute=True,now=NOW)
    before=m.load_business_evidence(factory,['600519.SH'],NOW.date())
    for _ in range(5):
        report=m.capture(factory,Client(cap=True),execute=True,now=NOW+timedelta(days=8))
        if report['status']=='blocked':break
    assert report['code']=='BUSINESS_ROW_CAP_EXCEEDED'
    assert m.load_business_evidence(factory,['600519.SH'],NOW.date())==before


def test_oversized_first_response_stops_before_second_supplier_request(factory):
    m=module();client=Client(too_many=True)
    assert m.capture(factory,client,execute=True,now=NOW)['status']=='blocked'
    assert len(client.calls)==1


def test_actual_150_row_success_is_split_not_rejected_as_protocol_error(factory):
    class Actual(Client):
        def call(self,api,params,fields):
            self.calls.append((api,deepcopy(params),tuple(fields)))
            periods=[f'{y}{suffix}' for y in range(int(params['start_date'][:4]),int(params['end_date'][:4])+1) for suffix in ('0630','1231') if params['start_date']<=f'{y}{suffix}'<=params['end_date']]
            rows=[[params['ts_code'],p,'产品'+str(i),100,20,80,'CNY','0'] for p in periods for i in range(10)]
            data=dict(fields=list(fields),items=rows[:150],has_more=len(rows)>150)
            body=json.dumps(dict(code=0,data=data)).encode()
            self.observer(dict(statusCode=200,credentialsRedacted=False,bodyBase64=base64.b64encode(body).decode(),bodySha256=sha256(body).hexdigest(),byteLength=len(body)))
            return TushareResponse.model_validate(data)
    m=module();client=Actual()
    result=m.capture(factory,client,execute=True,now=NOW)
    assert result['status']=='completed' and result['requestCount']>2
    assert len(m.load_business_evidence(factory,['600519.SH'],NOW.date())['600519.SH']['rows'])==400


@pytest.mark.parametrize('damage',[None,'receipt','original','unit'])
def test_known_old_oversize_failure_only_adopts_navigation_preserving_original(factory,damage):
    m=module();client=Client(split=True)
    # 先保存旧100行硬上限校验所产生的150行失败原件。
    from app.portfolio.reference_catalog import capture_catalog
    from app.ingestion.repositories import content_hash
    plan=m.capture(factory,now=NOW)
    with factory() as s:source=m._scope(s)
    c=source|dict(schemaVersion=1,api='fina_mainbz',fields=list(m.FIELDS),types=list(m.TYPES),rowCap=100,
        startDate=m._start(NOW.date(),10).isoformat(),endDate=NOW.date().isoformat(),historyYears=10,refreshDays=7,cycle=NOW.isoformat())
    spec=m._spec('600519.SH','P',c['startDate'],c['endDate'])
    class Old(Client):
        def call(self,api,params,fields):
            self.calls.append((api,deepcopy(params),tuple(fields)))
            data=dict(fields=list(fields),items=[[params['ts_code'],'20251231','项目A',100,20,80,'CNY','0']]*150,has_more=True)
            body=json.dumps(dict(code=0,data=data)).encode()
            self.observer(dict(statusCode=200,credentialsRedacted=False,bodyBase64=base64.b64encode(body).decode(),bodySha256=sha256(body).hexdigest(),byteLength=len(body)))
            return TushareResponse.model_validate(data)
    def old_limit(response,spec):
        if len(response.items)>100:raise ValueError('旧的硬上限')
    capture_catalog(factory,Old(),execute=True,_job=m.JOB,_requests=[spec],_contract=c,_control_key='contract:'+content_hash(c),_config={'maxHttpPerRun':1},_validator=old_limit)
    with factory() as s:
        failed=s.scalar(select(JobCursor).where(JobCursor.cursor_key==content_hash(c)+':'+spec['target']))
        before=deepcopy(failed.cursor_value);raw=s.get(SourcePayload,before['rawId']);original=deepcopy(raw.payload)
        batch=s.get(IngestionBatch,before['batchId']);batch_before=(batch.status,batch.rejected_count,batch.row_count)
    with factory() as s:count=s.scalar(select(func.count()).select_from(JobCursor))
    preview=m.capture(factory,now=NOW)
    assert preview['status']=='planned' and preview['requestCount']==0
    with factory() as s:assert s.scalar(select(func.count()).select_from(JobCursor))==count
    result=m.capture(factory,client,execute=True,now=NOW)
    assert result['status']=='completed'
    assert not any(params==spec['params'] for _,params,_ in client.calls)
    with factory() as s:
        assert s.get(JobCursor,failed.id).cursor_value==before
        assert s.get(SourcePayload,before['rawId']).payload==original
        batch=s.get(IngestionBatch,before['batchId']);assert (batch.status,batch.rejected_count,batch.row_count)==batch_before
        receipts=list(s.scalars(select(JobCursor).where(JobCursor.cursor_key.like('split-parent:%'))))
        assert len(receipts)==1 and receipts[0].cursor_value['consumable'] is False
    assert all(r['bz_sales']==100 for r in m.load_business_evidence(factory,['600519.SH'],NOW.date())['600519.SH']['rows'])
    if damage:
        with factory.begin() as s:
            if damage=='receipt':
                receipt=s.scalar(select(JobCursor).where(JobCursor.cursor_key.like('split-parent:%')))
                receipt.cursor_value=receipt.cursor_value|{'consumable':True}
            elif damage=='original':
                raw=s.get(SourcePayload,before['rawId']);raw.payload=raw.payload|{'_http':raw.payload['_http']|{'bodySha256':'bad'}}
            else:s.delete(s.get(JobCursor,failed.id))
        requests=len(client.calls)
        preview=m.capture(factory,now=NOW)
        assert preview['status']=='blocked' and preview['requestCount']==0
        assert m.capture(factory,client,execute=True,now=NOW)['status']=='blocked'
        assert len(client.calls)==requests
        with pytest.raises(ValueError):m.load_business_evidence(factory,['600519.SH'],NOW.date())
