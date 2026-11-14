from copy import deepcopy
from datetime import date,datetime,timedelta
from importlib import import_module

import pytest

from backend.tests.macro.test_exchange_summary_config import policy


def calendar(start=date(2026,10,5),end=date(2026,11,6),holidays=()):
    """受控日历，不取代真实交易日数据；节假日须显式给定。"""
    rows=[];previous=start-timedelta(days=1)
    while previous.weekday()>4 or previous in holidays:previous-=timedelta(days=1)
    day=start
    while day<=end:
        opened=day.weekday()<5 and day not in holidays
        for exchange in ('SSE','SZSE'):
            rows.append({'exchange':exchange,'date':day.isoformat(),'open':opened,'previous':previous.isoformat()})
        if opened:previous=day
        day+=timedelta(days=1)
    return rows


def build(at='2026-10-26T00:15:00+08:00',group='marginBalances',rows=None,p=None):
    return import_module('app.macro.exchange_summary_plan').build_exchange_plan(group,policy(group) if p is None else p,
        calendar() if rows is None else rows,datetime.fromisoformat(at))


def test_current_due_uses_last_released_common_day_not_latest_open_or_computer_date():
    p=build()
    assert p['targetDate']=='2026-10-22' and p['budgetPeriod']=='target-trade-date'
    assert p['releaseWindow']=={'startAt':'2026-10-23T10:00:00+08:00','nextAt':'2026-10-26T10:00:00+08:00'}
    assert p['startPeriod']=='2022-11-01' and p['endPeriod']=='2026-10-01'
    assert [d['query']['params'] for d in p['documents']]==[{'trade_date':'20261022','exchange_id':e} for e in ('SSE','SZSE','BSE')]
    assert p==build('2026-10-26T18:59:59+08:00')
    assert 'scheduledAt' not in p and 'checkedAt' not in p
    import_module('app.macro.exchange_summary_plan').validate_exchange_plan(p)


def test_new_due_after_monday_release_selects_friday_with_no_need_to_wait_for_task():
    p=build('2026-10-26T19:00:00+08:00')
    assert p['targetDate']=='2026-10-23'
    assert p['releaseWindow']['startAt']=='2026-10-26T10:00:00+08:00'
    assert p['releaseWindow']['nextAt']=='2026-10-27T10:00:00+08:00'
    assert p['fingerprint']!=build()['fingerprint']


def test_source_publication_clock_remains_shanghai_when_operator_schedule_is_utc():
    value=policy();value['schedule']['timezone']='UTC';value['schedule']['time']='12:00'
    p=build('2026-10-26T12:01:00+00:00',p=value)
    assert p['targetDate']=='2026-10-23' and p['releaseWindow']['startAt'].endswith('10:00:00+08:00')


def test_explicit_holiday_moves_release_to_actual_next_open_without_weekday_guess():
    rows=calendar(holidays=(date(2026,10,26),))
    p=build('2026-10-26T19:00:00+08:00',rows=rows)
    assert p['targetDate']=='2026-10-22' and p['releaseWindow']['nextAt']=='2026-10-27T10:00:00+08:00'


@pytest.mark.parametrize('mutation',['missing','conflict','duplicate','bad-previous','bad-date','coerce-open','coerce-date','out-of-range'])
def test_incomplete_inconsistent_or_noncanonical_calendar_is_readonly_blocked(mutation):
    rows=calendar()
    i=next(i for i,r in enumerate(rows) if r['exchange']=='SZSE' and r['date']=='2026-10-23')
    if mutation=='missing':rows.pop(i)
    elif mutation=='conflict':rows[i]['open']=False
    elif mutation=='duplicate':rows.append(deepcopy(rows[i]))
    elif mutation=='bad-previous':rows[i]['previous']='2026-10-20'
    elif mutation=='bad-date':rows[i]['date']='2026-10-32'
    elif mutation=='coerce-open':rows[i]['open']=1
    elif mutation=='coerce-date':rows[i]['date']=20261023
    else:rows=calendar(start=date(2020,1,1))
    with pytest.raises(ValueError):build(rows=rows)


@pytest.mark.parametrize('end',[date(2026,10,22),date(2026,10,25)])
def test_missing_future_release_day_is_not_assumed_open_or_old_target_reused(end):
    with pytest.raises(ValueError):build(rows=calendar(end=end))


def test_frozen_calendar_proof_only_covers_target_to_next_release_and_reconstructs():
    p=build();proof=p['calendarEvidence'];rows=proof['rows']
    assert proof['source']=='core.trade_calendar'
    assert min(r['date'] for r in rows)=='2026-10-22' and max(r['date'] for r in rows)=='2026-10-26'
    assert len(rows)==10 and len(proof['fingerprint'])==64
    value=policy();value['collectionEnabled']=True
    assert build(p=value)==p
    value['limits']['maxHttpPerRun']=1
    assert build(p=value)['fingerprint']!=p['fingerprint']


def test_denominator_query_and_plan_identity_are_independent_of_margin_budget():
    p=build(group='aShareFloatMv')
    assert p['targetDate']==build()['targetDate']
    assert [d['query']['params'] for d in p['documents']]==[
        {'trade_date':'20261022','ts_code':'SH_A','exchange':'SH'},
        {'trade_date':'20261022','ts_code':'SZ_A','exchange':'SZ'}]
    assert p['limits']['maxHttpPerPeriod']==2


@pytest.mark.parametrize('field',['query','target','calendar','window','budget','extra'])
def test_rehashing_a_tampered_plan_never_authorizes_it(field):
    p=build();module=import_module('app.macro.extensions.plan')
    if field=='query':p['documents'][0]['query']['params']['trade_date']='20261023'
    elif field=='target':p['targetDate']='2026-10-23'
    elif field=='calendar':p['calendarEvidence']['rows'][0]['open']=False
    elif field=='window':p['releaseWindow']['startAt']='2026-10-22T10:00:00+08:00'
    elif field=='budget':p['limits']['maxHttpPerPeriod']=5
    else:p['extra']=True
    p['fingerprint']=module.digest({k:v for k,v in p.items() if k!='fingerprint'})
    with pytest.raises(ValueError):import_module('app.macro.exchange_summary_plan').validate_exchange_plan(p)


def test_naive_clock_is_not_a_real_cutoff():
    with pytest.raises(ValueError):build('2026-10-26T00:00:00')
