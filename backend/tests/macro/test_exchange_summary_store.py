from copy import deepcopy
from datetime import datetime,timedelta,timezone
from importlib import import_module

import pytest
from sqlalchemy import select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobCursor,JobRun
from app.db.models.raw import IngestionBatch,SourcePayload
from backend.tests.macro.test_extension_store import database,count
from backend.tests.macro.test_exchange_summary_plan import build
from backend.tests.macro.test_exchange_summary_normalize import business

NOW=datetime(2026,10,25,16,30,tzinfo=timezone.utc)


def store(factory):return import_module('app.macro.exchange_summary_store').ExchangeSummaryStore(factory)


def complete(s,p,doc,now=NOW,data=None):
    key=s.reserve(p,doc,now)
    s.receive(key,business(p,'123.5') if data is None else data,now+timedelta(seconds=1))
    s.replay(key,now+timedelta(seconds=2))
    return key


def test_durable_raw_first_core_completion_and_zero_http_replay(database):
    _,f=database;s=store(f);p=build();doc=p['documents'][0]
    key=s.reserve(p,doc,NOW)
    assert s.inspect(p,NOW)['status']=='blocked' and count(f,SourcePayload)==0
    s.receive(key,business(p,'123.5'),NOW+timedelta(seconds=1))
    assert count(f,SourcePayload)==1 and count(f,MacroObservation)==0
    s.replay(key,NOW+timedelta(seconds=2))
    with f() as session:
        core=session.scalar(select(MacroObservation))
        assert core.series_id==doc['seriesId'] and core.released_at.replace(tzinfo=timezone.utc)==NOW+timedelta(seconds=2)
    assert s.replay(key,NOW+timedelta(seconds=3))['readOnly']
    with pytest.raises(ValueError):s.reserve(p,doc,NOW+timedelta(seconds=4))


@pytest.mark.parametrize('failed',[False,True])
def test_unknown_and_failed_freeze_target_across_days_without_refund(database,failed):
    _,f=database;s=store(f);p=build();key=s.reserve(p,p['documents'][0],NOW)
    if failed:s.fail(key,'EXTENSION_PERMISSION_ERROR',NOW+timedelta(seconds=1))
    later=NOW+timedelta(days=1);other=build('2026-10-26T19:00:00+08:00')
    assert s.inspect(other,later)['plan']==p
    with pytest.raises(ValueError):s.reserve(other,other['documents'][0],later)
    assert count(f,JobRun)==count(f,JobCursor)==1


def test_empty_and_null_complete_and_next_target_can_progress(database):
    _,f=database;s=store(f);p=build()
    for i,doc in enumerate(p['documents']):
        data={'fields':doc['query']['fields'],'items':[]} if i<2 else {'fields':doc['query']['fields'],'items':[['20261022','BSE',None]]}
        complete(s,p,doc,NOW+timedelta(seconds=i*3),data)
    assert s.inspect(p,NOW+timedelta(seconds=10))['status']=='completed' and count(f,MacroObservation)==0
    later=NOW+timedelta(days=1);q=build('2026-10-26T19:00:00+08:00')
    assert s.inspect(q,later)['status']=='new'
    s.reserve(q,q['documents'][0],later)


def test_invalid_saved_business_retained_for_zero_http_replay_only(database):
    _,f=database;s=store(f);p=build();key=s.reserve(p,p['documents'][0],NOW)
    bad=business(p);bad['items'][0][1]='WRONG'
    s.receive(key,bad,NOW+timedelta(seconds=1))
    with pytest.raises(ValueError):s.replay(key,NOW+timedelta(seconds=2))
    assert s.inspect(p,NOW+timedelta(seconds=2))['documents'][0]['phase']=='received'
    with pytest.raises(ValueError):s.reserve(p,p['documents'][1],NOW+timedelta(seconds=3))
    assert count(f,SourcePayload)==1 and count(f,MacroObservation)==0


@pytest.mark.parametrize('field',['raw','binding','core','batch','normalized','root','clock','policy'])
def test_tampered_proof_and_clock_or_policy_drift_refuse_send(database,field):
    _,f=database;s=store(f);p=build();key=complete(s,p,p['documents'][0]);at=NOW+timedelta(seconds=5)
    with f.begin() as session:
        row=session.get(JobRun,key);d=deepcopy(row.details)
        if field=='raw':
            raw=session.get(SourcePayload,d['rawId']);raw.content_hash='0'*64
        elif field=='binding':d['coreBindings'][0]['value']='99';row.details=d
        elif field=='core':session.scalar(select(MacroObservation)).value=99
        elif field=='batch':session.get(IngestionBatch,key).request_count=0
        elif field=='normalized':d['normalized']['status']='null';row.details=d
        elif field=='root':session.delete(session.scalar(select(JobCursor)))
        elif field=='clock':at=NOW-timedelta(seconds=1)
        else:
            from backend.tests.macro.test_exchange_summary_config import policy
            value=policy();value['limits']['maxHttpPerRun']=1;p=build(p=value)
    with pytest.raises(ValueError):s.inspect(p,at)


def test_out_of_order_and_other_group_are_independent(database):
    _,f=database;s=store(f);p=build()
    with pytest.raises(ValueError):s.reserve(p,p['documents'][1],NOW)
    s.reserve(p,p['documents'][0],NOW)
    q=build(group='aShareFloatMv');s.reserve(q,q['documents'][0],NOW)
    assert count(f,JobRun)==2


def test_unused_old_target_cannot_be_opened_after_normal_due_has_advanced(database):
    _,f=database;s=store(f);p=build()
    with pytest.raises(ValueError):s.reserve(p,p['documents'][0],NOW+timedelta(days=1))
    assert count(f,JobCursor)==count(f,JobRun)==0
