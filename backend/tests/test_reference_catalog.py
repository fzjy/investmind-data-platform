from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db.models import JobCursor,IngestionBatch,SourcePayload

def factory():
    e=create_engine('sqlite://',execution_options={'schema_translate_map':{'ops':None,'raw':None}})
    for m in (JobCursor,IngestionBatch,SourcePayload):m.__table__.create(e)
    return sessionmaker(e,expire_on_commit=False)

def test_default_preview_never_calls_provider():
    from app.portfolio.reference_catalog import capture_catalog
    f=factory()
    assert capture_catalog(f)['requestCount']==0

def test_reservation_survives_failure_and_never_implicitly_retries():
    from app.portfolio.reference_catalog import capture_catalog
    f=factory()
    class Client:
        def set_response_observer(self,observer):pass
        def call(self,*args):raise ValueError('failed')
    r=capture_catalog(f,Client(),execute=True)
    assert r['requestCount']==2 and r['failedCount']==2
    assert capture_catalog(f,Client(),execute=True)['requestCount']==0

def test_original_saved_and_reentry_is_zero_even_when_directory_is_empty():
    from app.portfolio.reference_catalog import capture_catalog,FIELDS
    from app.connectors.tushare.contracts import TushareResponse
    from hashlib import sha256
    import base64,json
    f=factory()
    class Client:
        def set_response_observer(self,observer):self.observer=observer
        def call(self,*args):
            body=json.dumps({'code':0,'data':{'fields':list(FIELDS),'items':[]}}).encode()
            self.observer(dict(statusCode=200,bodyBase64=base64.b64encode(body).decode(),bodySha256=sha256(body).hexdigest(),byteLength=len(body),credentialsRedacted=False))
            return TushareResponse(fields=FIELDS,items=())
    r=capture_catalog(f,Client(),execute=True)
    assert r['catalogReady'] is False and r['requestCount']==2
    assert capture_catalog(f,Client(),execute=True)['requestCount']==0

def test_installed_package_without_checkout_has_bounded_defaults(monkeypatch,tmp_path):
    import app.portfolio.reference_catalog as module
    monkeypatch.setattr(module,'CONFIG_FILE',tmp_path/'no-repository'/'portfolio.json')
    assert module.capture_catalog(factory())['requestCount']==0

def test_frozen_contract_change_cannot_consume_more_requests():
    from app.portfolio.reference_catalog import capture_catalog
    f=factory()
    # 无请求列表的显式空计划只持久冻结合同，不产生供应商调用。
    assert capture_catalog(f,execute=True,_requests=[],_job='test-reference-freeze',_contract={'asOf':'2026-10-21'})['requestCount']==0
    r=capture_catalog(f,execute=True,_requests=[],_job='test-reference-freeze',_contract={'asOf':'2026-10-22'})
    assert r['status']=='blocked' and r['requestCount']==0
