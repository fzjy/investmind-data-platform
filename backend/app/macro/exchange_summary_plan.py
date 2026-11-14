"""同一目标交易日的闭合请求；日历证明和预算身份不随检查时间漂移。"""
from datetime import date,datetime,time,timedelta
from zoneinfo import ZoneInfo

from app.macro.exchange_summary_config import validate_exchange_policy
from app.macro.extensions.plan import canonical,digest,scheduled_due

NORMALIZER='exchange-summary-v1'
SERIES={
    'marginBalances':('cn.market.margin_balance.sse','cn.market.margin_balance.szse','cn.market.margin_balance.bse'),
    'aShareFloatMv':('cn.market.a_share_float_mv.sh','cn.market.a_share_float_mv.sz'),
}


def _day(value):
    if not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value:
        raise ValueError('EXCHANGE_CALENDAR_INVALID')
    return date.fromisoformat(value)


def _calendar(rows):
    if not isinstance(rows,list) or not 6<=len(rows)<=244:
        raise ValueError('EXCHANGE_CALENDAR_INVALID')
    by_day={}
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'exchange','date','open','previous'}:
            raise ValueError('EXCHANGE_CALENDAR_INVALID')
        day=_day(row['date'])
        previous=None if row['previous'] is None else _day(row['previous'])
        if (row['exchange'] not in ('SSE','SZSE') or type(row['open']) is not bool
            or previous is not None and previous>=day):
            raise ValueError('EXCHANGE_CALENDAR_INVALID')
        pair=by_day.setdefault(day,{})
        if row['exchange'] in pair:
            raise ValueError('EXCHANGE_CALENDAR_INVALID')
        pair[row['exchange']]=row
    days=sorted(by_day)
    if len(days)!=(days[-1]-days[0]).days+1:
        raise ValueError('EXCHANGE_CALENDAR_INCOMPLETE')
    last=None;ordered=[];opened=[]
    for i,day in enumerate(days):
        pair=by_day[day]
        if set(pair)!={'SSE','SZSE'}:
            raise ValueError('EXCHANGE_CALENDAR_INCOMPLETE')
        a,b=pair['SSE'],pair['SZSE']
        if (a['open']!=b['open'] or a['previous']!=b['previous']
            or i and a['previous']!=(None if last is None else last.isoformat())):
            raise ValueError('EXCHANGE_CALENDAR_CONFLICT')
        if i==0:last=None if a['previous'] is None else _day(a['previous'])
        if a['open']:last=day;opened.append(day)
        ordered.extend((a,b))
    return ordered,days,opened


def _release(day,policy):
    return datetime.combine(day,time.fromisoformat(policy.publicationTime),ZoneInfo(policy.publicationTimezone))


def _target_plan(group_id,policy,proof,target):
    checked=validate_exchange_policy(group_id,{**policy,'collectionEnabled':False})
    rows,days,opened=_calendar(proof)
    lag=checked.publicationLagTradingDays
    if (not opened or opened[0]!=target or days[0]!=target or len(opened)!=lag+2
        or days[-1]!=opened[-1]):
        raise ValueError('EXCHANGE_CALENDAR_PROOF_INVALID')
    frozen=checked.model_dump(exclude={'collectionEnabled'})
    month_index=target.year*12+target.month-1
    def month(index):return f'{index//12:04}-{index%12+1:02}-01'
    value={'schemaVersion':1,'normalizerVersion':NORMALIZER,'groupId':group_id,
        'groupPolicy':frozen,'groupPolicyHash':digest({'groupId':group_id,'policy':frozen,'normalizer':NORMALIZER}),
        'budgetPeriod':'target-trade-date','targetDate':target.isoformat(),
        'startPeriod':month(month_index-checked.lookbackPeriods+1),'endPeriod':month(month_index),
        'releaseWindow':{'startAt':_release(opened[lag],checked).isoformat(),
                         'nextAt':_release(opened[lag+1],checked).isoformat()},
        'calendarEvidence':{'source':'core.trade_calendar','rows':rows,'fingerprint':digest(rows)},
        'documents':[{'id':request.id,'seriesId':SERIES[group_id][i],
            'query':{'api':checked.apiName,'params':{'trade_date':target.strftime('%Y%m%d'),**request.params},
                     'fields':checked.fields}}
            for i,request in enumerate(checked.requests)],'limits':checked.limits.model_dump()}
    return {**value,'fingerprint':digest(value)}


def build_exchange_plan(group_id,policy,calendar_rows,now):
    checked=validate_exchange_policy(group_id,policy)
    due=scheduled_due(checked.schedule,now).astimezone(ZoneInfo(checked.publicationTimezone))
    rows,days,opened=_calendar(calendar_rows)
    low=due.date()-timedelta(days=checked.calendarLookbackDays)
    high=due.date()+timedelta(days=checked.calendarLookaheadDays)
    if days[0]<low or days[-1]>high or not days[0]<=due.date()<=days[-1]:
        raise ValueError('EXCHANGE_CALENDAR_WINDOW_INVALID')
    lag=checked.publicationLagTradingDays
    for i in range(len(opened)-lag-1):
        if _release(opened[i+lag],checked)<=due<_release(opened[i+lag+1],checked):
            proof=[r for r in rows if opened[i].isoformat()<=r['date']<=opened[i+lag+1].isoformat()]
            return _target_plan(group_id,policy,proof,opened[i])
    raise ValueError('EXCHANGE_RELEASE_NOT_PROVEN')


def validate_exchange_plan(plan):
    try:
        if not isinstance(plan,dict) or 'collectionEnabled' in plan['groupPolicy']:
            raise ValueError()
        expected=_target_plan(plan['groupId'],plan['groupPolicy'],plan['calendarEvidence']['rows'],_day(plan['targetDate']))
        if canonical(plan)!=canonical(expected):raise ValueError()
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise ValueError('EXCHANGE_PLAN_INVALID') from None
