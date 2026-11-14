"""公开账本合同：拒绝缺口/金额/成员/算式自相矛盾的观察。"""
from datetime import date
from math import isfinite, isclose
import re

from app.macro.enterprise import MAX_SAFE_INTEGER
from app.macro.enterprise_ledger import ledger_value


def valid_ledger(value: object, analysis: dict) -> bool:
    def number(v): return type(v) in (int,float) and isfinite(v)
    def integer(v,maximum=MAX_SAFE_INTEGER): return type(v) is int and 0<=v<=maximum
    def ordered(v): return isinstance(v,list) and all(isinstance(s,str) for s in v) and v==sorted(set(v))
    def close(a,b): return number(a) and isclose(a,b,rel_tol=1e-10,abs_tol=0)
    def period(v):
        if not isinstance(v,str): return False
        d=date.fromisoformat(v)
        return d.isoformat()==v and (d.month,d.day) in ((3,31),(6,30),(9,30),(12,31)) and v<analysis['asOfDate']
    try:
        definitions=[('pricing-power','定价权','百分点'),('cash-conversion','利润变现','倍')]
        if analysis['algorithmVersion']=='enterprise-quarter-v7': definitions.append(('order-temperature','订单温度','%'))
        if not isinstance(value,dict) or not isinstance(value['metrics'],list) or len(value['metrics'])!=len(definitions): return False
        calculation=value.get('calculationVersion')
        if 'calculationVersion' in value and calculation!='exact-zero-margin-v1': return False
        symbols=analysis['symbols'];policy=analysis['policy']
        for m,(identifier,title,unit) in zip(value['metrics'],definitions):
            orders=identifier=='order-temperature'
            if (m['id'],m['title'],m['unit'])!=(identifier,title,unit): return False
            points=m['points'];qualities=m['quality']
            if not isinstance(points,list) or not isinstance(qualities,list) or len(qualities)>policy['max_quarters']: return False
            if not ordered([p['period'] for p in points]) or not ordered([q['period'] for q in qualities]): return False
            ready={};previous=None
            for q in qualities:
                if not period(q['period']) or not ordered(q['included']): return False
                for key in ('excluded','nonApplicable'):
                    if not isinstance(q[key],dict) or any(not isinstance(k,str) or not isinstance(v,str) or not v for k,v in q[key].items()): return False
                members=[*q['included'],*q['excluded'],*q['nonApplicable']]
                if len(set(members))!=len(members) or set(members)!=set(symbols): return False
                n=q['sampleCount'];total=q['candidateCount']
                if not integer(n,len(symbols)) or not integer(total,len(symbols)) or n!=len(q['included']) or total!=len(symbols)-len(q['nonApplicable']): return False
                if not close(q['coverage'],n*100/total if total else 0): return False
                for key in ('negativeIndividualBases','zeroIndividualBases','negativeQuarterCosts'):
                    if not ordered(q[key]) or not set(q[key])<=set(q['included']): return False
                if identifier in ('cash-conversion','order-temperature') and q['negativeQuarterCosts']: return False
                if orders and (not ordered(q['zeroContractLiabilityBases']) or not set(q['zeroContractLiabilityBases'])<=set(q['included'])): return False
                if identifier=='cash-conversion' and set(q['negativeIndividualBases'])&set(q['zeroIndividualBases']): return False
                if not isinstance(q['provenanceHash'],str) or not re.fullmatch('[0-9a-f]{64}',q['provenanceHash']) or not integer(q['sourceRefCount']): return False
                expected=('no-candidates' if not total else 'insufficient-samples' if n<policy['min_samples']
                    else 'insufficient-coverage' if n*100<total*policy['min_coverage'] else None)
                if expected is not None:
                    if q['status']!=expected: return False
                elif q['status'] not in ('ready','non-finite-aggregate'): return False
                if q['status']=='ready':
                    ready[q['period']]=(q,previous is not None and previous!=q['included']);previous=q['included']
            if set(ready)!={p['period'] for p in points}: return False
            keys={'contractLiabilities','baseContractLiabilities','revenue','baseRevenue'} if orders else {'revenue','cost','baseRevenue','baseCost'} if identifier=='pricing-power' else {'operatingCashflow','netIncome'}
            for p in points:
                if not period(p['period']): return False
                d=date.fromisoformat(p['period']);q,changed=ready[p['period']]
                if p['quarter']!=f'{d.year}Q{d.month//3}' or not isinstance(p['amounts'],dict) or set(p['amounts'])!=keys or not all(number(v) for v in p['amounts'].values()): return False
                a=p['amounts'];negative=q['negativeIndividualBases'];zero=q['zeroIndividualBases']
                if identifier=='cash-conversion':
                    profit=a['netIncome'];nonpositive=len(negative)+len(zero)
                    if ((not negative and profit<0) or (not negative and not zero and profit<=0)
                        or (nonpositive==q['sampleCount'] and (profit>=0 if negative else profit!=0))): return False
                elif not orders:
                    if not q['negativeQuarterCosts'] and (a['cost']<0 or a['baseCost']<0): return False
                if identifier in ('pricing-power','order-temperature'):
                    if not negative and (a['revenue']<0 or a['baseRevenue']<0): return False
                    if not negative and not zero and (a['revenue']<=0 or a['baseRevenue']<=0): return False
                if orders:
                    if (a['contractLiabilities']<0 or a['baseContractLiabilities']<0
                        or (a['baseContractLiabilities']==0)!=(len(q['zeroContractLiabilityBases'])==q['sampleCount'])): return False
                    from app.macro.enterprise_orders import order_values
                    calculated=order_values(a);expected=calculated['value'];status=calculated['status']
                    if p['revenueYoYStatus']!=calculated['revenueYoYStatus']: return False
                    for key in ('revenueYoY','orderRevenueSpread'):
                        if p[key] is not None if calculated[key] is None else not close(p[key],calculated[key]): return False
                else: expected,status=ledger_value(identifier,p['amounts'],legacy=calculation is None)
                if p['status']!=status or (p['value'] is not None if expected is None else not close(p['value'],expected)): return False
                if type(p['sampleSetChanged']) is not bool or p['sampleSetChanged']!=changed: return False
                if not integer(p['sampleCount']) or not integer(p['candidateCount']) or p['sampleCount']!=q['sampleCount'] or p['candidateCount']!=q['candidateCount'] or not close(p['coverage'],q['coverage']): return False
        return True
    except (KeyError,TypeError,ValueError,OverflowError,AttributeError):
        return False
