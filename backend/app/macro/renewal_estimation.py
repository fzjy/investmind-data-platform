"""三项折旧的实报优先选择及显式估算证明；不读取数据库或覆盖原件。"""
from copy import deepcopy
from datetime import date
from fractions import Fraction
import re

from app.macro.extensions.plan import canonical, digest

FIELDS=('depr_fa_coga_dpba','amort_intang_assets','lt_amort_deferred_exp')
MODE='reported-quarter-priority-explicit-estimates-v1'
MISSING={'missing-report','missing-input','rea-supplement-returned-null','rea-supplement-missing-field'}


def quarter(value):
    if type(value) is not str:raise ValueError('RENEWAL_PERIOD_INVALID')
    d=date.fromisoformat(value)
    if d.isoformat()!=value or (d.month,d.day) not in ((3,31),(6,30),(9,30),(12,31)):
        raise ValueError('RENEWAL_PERIOD_INVALID')
    return d


def candidate_periods(target):
    quarter(target.isoformat())
    result=[date(target.year,m,31 if m in (3,12) else 30).isoformat() for m in range(target.month,0,-3)]
    if target.year>1:result.append(date(target.year-1,12,31).isoformat())
    return result


def missing_observation(period):
    return {'period':period,'symbol':None,'availableDate':None,'sourceKey':None,'sourceHash':None,
        'fields':dict.fromkeys(FIELDS),'reason':'missing-report'}


def _validate_observation(value,period):
    if (type(value) is not dict or set(value)!={'period','symbol','availableDate','sourceKey','sourceHash','fields','reason'}
        or value['period']!=period or type(value['fields']) is not dict or set(value['fields'])!=set(FIELDS)
        or value['reason'] is not None and (type(value['reason']) is not str or not value['reason'])):
        raise ValueError('RENEWAL_OBSERVATION_INVALID')
    if value['sourceKey'] is None:
        if (value['symbol'] is not None or value['sourceHash'] is not None or value['availableDate'] is not None
            or any(v is not None for v in value['fields'].values()) or value['reason'] is None):
            raise ValueError('RENEWAL_OBSERVATION_INVALID')
    else:
        if (type(value['symbol']) is not str or not 1<=len(value['symbol'])<=32
            or type(value['sourceKey']) is not str or not 1<=len(value['sourceKey'])<=256
            or type(value['sourceHash']) is not str or not re.fullmatch('[0-9a-f]{64}',value['sourceHash'])
            or type(value['availableDate']) is not str
            or date.fromisoformat(value['availableDate']).isoformat()!=value['availableDate']):
            raise ValueError('RENEWAL_OBSERVATION_INVALID')


def _status(observation):
    values=[]
    for field in FIELDS:
        value=observation['fields'][field]
        if value is None:values.append(None);continue
        if type(value) is not str or len(value)>80 or not re.fullmatch(r'\d+(?:\.\d+)?',value):
            return None,'invalid-depreciation-field:'+field
        values.append(Fraction(value))
    reason=observation['reason']
    if reason is not None and not (reason in MISSING or reason.startswith('missing-field:')):
        return None,reason
    if any(v is None for v in values):return None,None
    if reason is not None:raise ValueError('RENEWAL_OBSERVATION_REASON_INVALID')
    return values,None


def choose_depreciation(target:date,observations:dict) -> dict:
    periods=candidate_periods(target);seen=[]
    def get(period):
        if period in [r['period'] for r in seen]:return next(r for r in seen if r['period']==period)
        value=deepcopy(observations(period) if callable(observations) else observations.get(period,missing_observation(period)))
        _validate_observation(value,period);seen.append(value);return value
    def result(method='unavailable',source=None,divisor=None,value=None,reason=None):
        return {'targetPeriod':target.isoformat(),'method':method,'sourcePeriod':source,'divisor':divisor,
            'estimated':method in ('cumulative-average','previous-year-average'),
            'value':None if value is None else {'numerator':str(value.numerator),'denominator':str(value.denominator)},
            'observations':deepcopy(seen),'reason':reason}
    current,error=_status(get(periods[0]))
    if error:return result(reason=error)
    if current is not None:
        if target.month==3:return result('reported-q1',periods[0],1,sum(current))
        previous,error=_status(get(periods[1]))
        if error:return result(reason=error)
        if previous is not None:
            differences=[a-b for a,b in zip(current,previous,strict=True)]
            if any(v<0 for v in differences):return result(reason='negative-quarter-depreciation')
            return result('reported-difference',periods[0],1,sum(differences))
        return result('cumulative-average',periods[0],target.month//3,sum(current)/(target.month//3))
    for period in periods[1:]:
        values,error=_status(get(period))
        if error:return result(reason=error)
        if values is not None:
            when=quarter(period);method='cumulative-average' if when.year==target.year else 'previous-year-average'
            return result(method,period,when.month//3,sum(values)/(when.month//3))
    return result(reason='missing-depreciation-source')


def decision_valid(value,as_of):
    try:
        if type(value) is not dict or type(value['observations']) is not list or len(value['observations'])>5:return False
        target=quarter(value['targetPeriod']);obs=value['observations'];dates=[o['period'] for o in obs]
        if len(dates)!=len(set(dates)) or any(d not in candidate_periods(target) for d in dates):return False
        if any(o['availableDate'] is not None and date.fromisoformat(o['availableDate'])>as_of for o in obs):return False
        expected=choose_depreciation(target,{o['period']:o for o in obs})
        return canonical(value)==canonical(expected)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,ArithmeticError):return False


def evidence_hash(period,members):
    hashes=[digest(m) for m in members]
    return digest({'version':'renewal-depreciation-v1','period':period,
        'memberChunkHashes':[digest(hashes[i:i+128]) for i in range(0,len(hashes),128)]})


def valid_evidence(value,period,included,as_of,amounts=None):
    """逐成员重建选择；重算摘要不能使伪造的算法结果通过。"""
    try:
        if type(value) is not dict or set(value)!={'version','members','estimatedMembers','evidenceHash'}:return False
        if value['version']!='renewal-depreciation-v1' or type(value['members']) is not list:return False
        members=value['members'];old=quarter(period).replace(year=quarter(period).year-1).isoformat()
        if [m['symbol'] for m in members]!=included:return False
        sums={'b':Fraction(0),'oldB':Fraction(0)};estimated=[]
        for member in members:
            if set(member)!={'symbol','current','yearAgo'}:return False
            for side,target,key in (('current',period,'b'),('yearAgo',old,'oldB')):
                d=member[side]
                if d['targetPeriod']!=target or not decision_valid(d,as_of) or d['value'] is None:return False
                if any(o['sourceKey'] is not None and o['symbol']!=member['symbol'] for o in d['observations']):return False
                sums[key]+=Fraction(int(d['value']['numerator']),int(d['value']['denominator']))
            if member['current']['estimated'] or member['yearAgo']['estimated']:estimated.append(member['symbol'])
        if value['estimatedMembers']!=estimated or value['evidenceHash']!=evidence_hash(period,members):return False
        if amounts is not None:
            from app.macro.enterprise_rea import _equal
            if any(not _equal(float(v),amounts[k]) for k,v in sums.items()):return False
        return True
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,ArithmeticError):return False
