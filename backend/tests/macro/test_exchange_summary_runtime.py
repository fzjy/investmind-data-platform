from copy import deepcopy
from datetime import timedelta
from importlib import import_module
import json

import httpx
from sqlalchemy import select

from app.db.models.core import MacroObservation,TradeCalendar
from app.db.models.ops import JobRun
from app.db.models.raw import SourcePayload
from app.macro.extensions.config import ExtensionConfig,load_extension_config
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_exchange_summary_config import policy
from backend.tests.macro.test_exchange_summary_plan import calendar
from backend.tests.macro.test_exchange_summary_store import NOW
from backend.tests.macro.test_exchange_summary_client import TOKEN,response


def setup(factory,monkeypatch):
    from datetime import date
    with factory.begin() as session:
        session.add_all([TradeCalendar(exchange=r['exchange'],cal_date=date.fromisoformat(r['date']),is_open=r['open'],
            pretrade_date=date.fromisoformat(r['previous'])) for r in calendar()])
    data=load_extension_config().model_dump()
    for group in ('marginBalances','aShareFloatMv'):
        data['groups'][group]=policy(group);data['groups'][group]['collectionEnabled']=True
    config=ExtensionConfig.model_validate(data)
    module=import_module('app.macro.jobs.exchange_summary_runtime')
    monkeypatch.setattr(module,'load_extension_config',lambda:config)
    return config,module


def execute(f,c,m,handler,group='marginBalances',clock=None,**kwargs):
    return m.execute_exchange_group(f,c,group,TOKEN,transport=httpx.MockTransport(handler),clock=clock or (lambda:NOW),**kwargs)


def handler_for(factory,calls,fail_bse=False,empty=False):
    def handler(request):
        query=json.loads(request.content);calls.append(query)
        assert count(factory,JobRun)==len(calls)  # 另一连接看到已提交的预留。
        if fail_bse and query['params'].get('exchange_id')=='BSE':return response(value={'code':2002,'msg':TOKEN})
        fields=query['fields'].split(',');items=[] if empty else [[query['params'].get(k,'123.5') for k in fields]]
        return response(value={'code':0,'request_id':'unneeded','data':{'fields':fields,'items':items}})
    return handler


def test_normal_entry_commits_before_http_and_same_target_reentry_zero(database,monkeypatch):
    _,f=database;c,m=setup(f,monkeypatch);calls=[];handler=handler_for(f,calls)
    result=execute(f,c,m,handler)
    assert result['status']=='completed' and result['supplierHttp']==3 and result['insertedRows']==3
    again=execute(f,c,m,handler,clock=lambda:NOW+timedelta(hours=10))
    assert again['readOnly'] and again['supplierHttp']==0 and len(calls)==3
    assert count(f,SourcePayload)==count(f,MacroObservation)==3


def test_bse_failure_keeps_shanghai_shenzhen_and_other_group_can_complete(database,monkeypatch):
    _,f=database;c,m=setup(f,monkeypatch);calls=[];handler=handler_for(f,calls,fail_bse=True)
    first=execute(f,c,m,handler)
    assert first['status']=='blocked' and first['supplierHttp']==3 and count(f,MacroObservation)==2
    other=execute(f,c,m,handler,'aShareFloatMv')
    assert other['status']=='completed' and other['supplierHttp']==2 and count(f,MacroObservation)==4
    later=execute(f,c,m,handler,clock=lambda:NOW+timedelta(days=1))
    assert later['status']=='blocked' and later['supplierHttp']==0 and len(calls)==5


def test_per_run_cap_preserves_partial_and_resumes_same_target_without_duplicate(database,monkeypatch):
    _,f=database;c,m=setup(f,monkeypatch);value=c.model_dump();value['groups']['marginBalances']['limits']['maxHttpPerRun']=1
    c=ExtensionConfig.model_validate(value);monkeypatch.setattr(m,'load_extension_config',lambda:c)
    calls=[];handler=handler_for(f,calls)
    assert execute(f,c,m,handler)['status']=='partial'
    assert execute(f,c,m,handler)['status']=='partial'
    assert execute(f,c,m,handler)['status']=='completed' and len(calls)==3


def test_disabled_or_changed_config_never_reserves_or_sends(database,monkeypatch):
    _,f=database;c,m=setup(f,monkeypatch);value=c.model_dump();value['groups']['marginBalances']['collectionEnabled']=False
    disabled=ExtensionConfig.model_validate(value);calls=[];handler=handler_for(f,calls)
    assert execute(f,disabled,m,handler)['status']=='disabled'
    monkeypatch.setattr(m,'load_extension_config',lambda:disabled)
    changed=execute(f,c,m,handler)
    assert changed['status']=='blocked' and changed['supplierHttp']==0 and count(f,JobRun)==0


def test_saved_raw_normalizer_failure_only_replays_no_supplier_diagnosis(database,monkeypatch):
    _,f=database;c,m=setup(f,monkeypatch);calls=[]
    def bad(request):
        calls.append(request)
        return response(value={'code':0,'data':{'fields':['trade_date','exchange_id','rzye'],'items':[['20261022','WRONG','1']]}})
    result=execute(f,c,m,bad)
    assert result['status']=='replay_pending' and result['supplierHttp']==1 and count(f,SourcePayload)==1
    again=execute(f,c,m,bad)
    assert again['status']=='replay_pending' and again['supplierHttp']==0 and len(calls)==1


def test_receive_commit_failure_freezes_unknown_without_retry(database,monkeypatch):
    _,f=database;c,m=setup(f,monkeypatch);calls=[]
    monkeypatch.setattr(m.ExchangeSummaryStore,'receive',lambda *args:(_ for _ in ()).throw(RuntimeError('unsafe detail')))
    result=execute(f,c,m,handler_for(f,calls))
    assert result['status']=='blocked' and result['supplierHttp']==1
    again=execute(f,c,m,handler_for(f,calls))
    assert again['supplierHttp']==0 and len(calls)==1


def test_existing_same_day_core_is_rejected_before_reservation_or_http(database,monkeypatch):
    from datetime import date
    _,f=database;c,m=setup(f,monkeypatch);calls=[]
    with f.begin() as session:
        session.add(MacroObservation(source='tushare',series_id='cn.market.margin_balance.sse',name='existing',region='CN',
            frequency='daily',unit='元',seasonal_adjustment='NSA',observed_at=date(2026,10,22),released_at=NOW-timedelta(seconds=1),
            revision='existing',value=100,source_url='https://tushare.pro/document/2?doc_id=58'))
    result=execute(f,c,m,handler_for(f,calls))
    assert result['status']=='blocked' and result['supplierHttp']==0 and calls==[] and count(f,JobRun)==0
