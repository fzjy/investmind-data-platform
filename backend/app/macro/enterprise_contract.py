"""企业样本公开合同；完整逐行引用仍由只读审计入口提供。"""
from datetime import date, datetime
from dataclasses import replace
import hashlib
import json
from math import isclose, isfinite
import re
from zoneinfo import ZoneInfo

from app.macro.enterprise import CARD_QUALITY_FIELDS, MAX_SAFE_INTEGER, EnterpriseInputs, EnterprisePolicy, build_enterprise_analysis


def public_enterprise_analysis(inputs: EnterpriseInputs, as_of: date, policy: EnterprisePolicy, *, rea_enabled: bool = False, industry_enabled: bool = False) -> dict:
    result=build_enterprise_analysis(inputs,as_of,policy)
    result['symbols']=sorted(inputs.symbols)
    for card in [*result['cards'],*result.get('ledger',{}).get('metrics',[])]:
        for q in card['quality']:
            # 新增诊断不是新来源；保留旧完整引用的摘要身份。
            evidence={k:v for k,v in q.items() if k!='fieldCoverage'}
            q['provenanceHash']=hashlib.sha256(json.dumps(evidence,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            q['sourceRefCount']=len(q['sourceRefs'])
            for key in ('sourceRefs','industryRefs','securityRefs'): del q[key]
            q.pop('valuationRefs',None)
    if rea_enabled:
        from app.macro.enterprise_rea import build_rea_projection
        result['rea']=build_rea_projection(inputs,as_of,policy,result)
    if industry_enabled:
        from app.macro.enterprise_industries import build_industry_comparison
        result['industryComparison']=build_industry_comparison(inputs,as_of,policy,result)
    return result


def enterprise_loader(factory, settings, *, stock_history_enabled=None):
    """继承专属范围/政策，每次捕获实际成员，不产生采集。"""
    if stock_history_enabled is None:
        from app.macro.stock_history_policy import load_stock_history_policy
        stock_history_enabled=load_stock_history_policy()
    from app.macro.enterprise_repository import load_enterprise_inputs, load_enterprise_loss_inputs, resolve_enterprise_selection
    from app.macro.enterprise_receivables import load_enterprise_receivables
    policy=EnterprisePolicy(settings.macro_enterprise_min_coverage,
        settings.macro_enterprise_min_samples,settings.macro_enterprise_max_quarters)
    timezone=ZoneInfo(settings.sync_timezone)
    def load(as_of: date | datetime):
        cutoff=as_of.astimezone(timezone).date() if isinstance(as_of,datetime) else as_of
        selection=resolve_enterprise_selection(factory,settings)
        inputs=load_enterprise_receivables(factory,load_enterprise_inputs(factory,selection),cutoff,policy,
            page_size=settings.tushare_page_size,timezone=settings.sync_timezone)
        inputs=replace(inputs,cashflow_enabled=True,distribution_enabled=True)
        inputs=replace(load_enterprise_loss_inputs(factory,inputs,cutoff,policy),ledger_enabled=True)
        from app.macro.balance_supplement import ORDERS
        inputs=load_enterprise_receivables(factory,inputs,cutoff,policy,
            page_size=settings.tushare_page_size,timezone=settings.sync_timezone,target=ORDERS)
        inputs=replace(inputs,orders_enabled=True,renewal_estimation_enabled=True)
        from app.macro.enterprise_rea_inputs import load_enterprise_rea_inputs
        inputs=load_enterprise_rea_inputs(factory,inputs,cutoff,policy,
            page_size=settings.tushare_page_size,timezone=settings.sync_timezone)
        if stock_history_enabled:
            from app.macro.stock_history_bridge import load_stock_history_bridge
            inputs=load_stock_history_bridge(factory,inputs,as_of,enabled=True)
        result=public_enterprise_analysis(inputs,cutoff,policy,rea_enabled=True,industry_enabled=True)
        from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
        from app.macro.enterprise_official_targets import load_official_witnesses,build_official_target_comparison,load_official_source_states
        catalog=load_enterprise_target_catalog()
        witnesses=load_official_witnesses(factory,as_of,catalog)
        official=build_official_target_comparison(inputs,cutoff,policy,result,catalog,witnesses,
            load_official_source_states(factory,as_of,catalog,witnesses) if catalog.catalog_member_policy else None)
        if official is not None:result['officialTargets']=official
        return result
    return load


def valid_enterprise_analysis(value: object) -> bool:
    def integer(v,low=0,high=MAX_SAFE_INTEGER): return type(v) is int and low<=v<=high
    def number(v): return type(v) in (int,float) and isfinite(v)
    def digest(v): return isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v) is not None
    def ordered_unique(v): return isinstance(v,list) and all(isinstance(s,str) for s in v) and v==sorted(set(v))
    def close(a,b): return number(a) and number(b) and isclose(a,b,rel_tol=1e-10,abs_tol=1e-9)
    def period(v):
        if not isinstance(v,str): return False
        d=date.fromisoformat(v)
        return d.isoformat()==v and (d.month,d.day) in ((3,31),(6,30),(9,30),(12,31))
    try:
        if not isinstance(value,dict): return False
        if (value['schemaVersion']!='1.0' or value['algorithmVersion'] not in ('enterprise-quarter-v1','enterprise-quarter-v2','enterprise-quarter-v3','enterprise-quarter-v4','enterprise-quarter-v5','enterprise-quarter-v6','enterprise-quarter-v7')
            or value['basis']!='current-pool-restated-history-not-strict-pit'
            or not isinstance(value['poolVersion'],str) or not value['poolVersion']
            or not digest(value['inputHash'])): return False
        cutoff=date.fromisoformat(value['asOfDate'])
        if cutoff.isoformat()!=value['asOfDate']: return False
        symbols=value['symbols']; policy=value['policy']
        if (not ordered_unique(symbols) or not integer(value['poolCount'],1) or len(symbols)!=value['poolCount']
            or not integer(policy['min_coverage'],1,100) or not integer(policy['min_samples'],1)
            or not integer(policy['max_quarters'],1,40)): return False
        cards=value['cards']
        ids=['scissors','inventory'];titles=['营收利润剪刀差','宏观库存钟']
        if value['algorithmVersion'] in ('enterprise-quarter-v2','enterprise-quarter-v3','enterprise-quarter-v4','enterprise-quarter-v5','enterprise-quarter-v6','enterprise-quarter-v7'):
            ids.append('white-strip');titles.append('宏观白条指数')
        if value['algorithmVersion'] in ('enterprise-quarter-v3','enterprise-quarter-v4','enterprise-quarter-v5','enterprise-quarter-v6','enterprise-quarter-v7'):
            ids.append('lying-flat');titles.append('企业躺平指数')
        if value['algorithmVersion'] in ('enterprise-quarter-v4','enterprise-quarter-v5','enterprise-quarter-v6','enterprise-quarter-v7'):
            ids.append('dividend');titles.append('分红挤出扩产')
        if value['algorithmVersion'] in ('enterprise-quarter-v5','enterprise-quarter-v6','enterprise-quarter-v7'):
            ids.append('loss');titles.append('亏损扩散度')
        if not isinstance(cards,list) or [c['id'] for c in cards]!=ids: return False
        for c,title in zip(cards,titles):
            cash=c['id']=='lying-flat'
            loss=c['id']=='loss'
            distribution=c['id']=='dividend';amount=cash or distribution
            if (c['title']!=title or c['frequency']!='quarterly' or c['primaryUnit']!=('亿元' if amount else '%')
                or c['secondaryUnit']!=('亿元' if amount else '%') or c['resultUnit']!=('亿元' if amount else '百分点')): return False
            if cash and c['ratioUnit']!='%': return False
            if distribution and c['yoyUnit']!='%': return False
            if loss and c['marketValueUnit']!='亿元': return False
            points=c['points']; qualities=c['quality']
            if not isinstance(points,list) or not isinstance(qualities,list) or len(qualities)>policy['max_quarters']: return False
            if not ordered_unique([p['period'] for p in points]) or not ordered_unique([q['period'] for q in qualities]): return False
            ready={}; previous=None
            for q in qualities:
                if not period(q['period']) or date.fromisoformat(q['period'])>=cutoff: return False
                included=q['included']; excluded=q['excluded']; na=q['nonApplicable']
                if (not ordered_unique(included) or not isinstance(excluded,dict) or not isinstance(na,dict)
                    or any(not isinstance(v,str) or not v for v in (*excluded.values(),*na.values()))): return False
                groups=[*included,*excluded,*na]
                if len(set(groups))!=len(groups) or set(groups)!=set(symbols): return False
                n=q['sampleCount']; total=q['candidateCount']
                if not integer(n,0,len(symbols)) or not integer(total,0,len(symbols)) or n!=len(included) or total!=len(symbols)-len(na): return False
                if not close(q['coverage'],n*100/total if total else 0): return False
                if 'fieldCoverage' in q:
                    fields=q['fieldCoverage']
                    if (not isinstance(fields,dict) or set(fields)!=set(CARD_QUALITY_FIELDS[c['id']])
                        or any(not integer(count,n,total) for count in fields.values())): return False
                    minimum_common=max(0,sum(fields.values())-(len(fields)-1)*total)
                    if n<minimum_common: return False
                for key in ('negativeIndividualBases','zeroIndividualBases'):
                    if not ordered_unique(q[key]) or not set(q[key])<=set(included): return False
                    if cash and q[key]: return False
                    if distribution and key=='negativeIndividualBases' and q[key]: return False
                if not digest(q['provenanceHash']) or not integer(q['sourceRefCount']): return False
                if loss:
                    from app.macro.enterprise_loss import valuation_anchor
                    anchor=valuation_anchor(date.fromisoformat(q['period']))
                    if q['valuationAnchor']!=anchor.isoformat(): return False
                    for key in ('lossMembers','declineMembers'):
                        if not ordered_unique(q[key]) or not set(q[key])<=set(included): return False
                    day=q['valuationDate'];vs=q['valuationStatus']
                    if vs=='ready':
                        if not isinstance(day,str) or date.fromisoformat(day).isoformat()!=day or not anchor<date.fromisoformat(day)<cutoff: return False
                    elif vs not in ('valuation-window-not-due','valuation-calendar-missing','valuation-calendar-conflict','valuation-day-not-complete') or day is not None or n:
                        return False
                    if vs!='ready' and any(q.get('fieldCoverage',{}).values()): return False
                expected=('no-candidates' if not total else 'insufficient-samples' if n<policy['min_samples']
                    else 'insufficient-coverage' if n*100<total*policy['min_coverage'] else None)
                if expected is not None:
                    if q['status']!=expected: return False
                elif q['status'] not in (('ready','non-finite-aggregate') if amount or loss else ('ready','non-positive-aggregate-base','non-finite-aggregate')): return False
                if q['status']=='ready':
                    ready[q['period']]=(q,previous is not None and previous!=included)
                    previous=included
            if set(ready)!={p['period'] for p in points}: return False
            for p in points:
                if not period(p['period']): return False
                d=date.fromisoformat(p['period']); q,changed=ready[p['period']]
                if p['quarter']!=f'{d.year}Q{d.month//3}': return False
                if not all(number(p[k]) for k in ('primary','secondary','result')) or not close(p['result'],p['primary']-p['secondary']): return False
                if cash:
                    if p['secondary']<0: return False
                    ratio=p['capitalExpenditureRatio']; ratio_status=p['ratioStatus']
                    if p['primary']<=0:
                        if ratio is not None or ratio_status!='non-positive-operating-cashflow': return False
                    else:
                        expected_ratio=p['secondary']/p['primary']*100
                        if not isfinite(expected_ratio):
                            if ratio is not None or ratio_status!='non-finite-ratio': return False
                        elif ratio_status!='ready' or not close(ratio,expected_ratio): return False
                if distribution:
                    base=p['distributionBase'];yoy=p['distributionYoY'];yoy_status=p['distributionYoYStatus']
                    if p['primary']<0 or p['secondary']<0 or not number(base) or base<0: return False
                    if base==0:
                        if yoy is not None or yoy_status!='zero-distribution-base': return False
                    else:
                        expected_yoy=(p['secondary']/base-1)*100
                        if not isfinite(expected_yoy):
                            if yoy is not None or yoy_status!='non-finite-yoy': return False
                        elif yoy_status!='ready' or not close(yoy,expected_yoy): return False
                if loss:
                    total=p['totalMarketValue'];losing=p['lossMarketValue'];declining=p['declineMarketValue']
                    if (q['valuationStatus']!='ready' or p['valuationDate']!=q['valuationDate'] or not number(total) or total<=0
                        or not all(number(v) and 0<=v<=total for v in (losing,declining))
                        or bool(losing)!=bool(q['lossMembers']) or bool(declining)!=bool(q['declineMembers'])): return False
                    for actual,expected in ((p['primary'],losing/total*100),(p['secondary'],declining/total*100)):
                        if not 0<=actual<=100 or not isclose(actual,expected,rel_tol=1e-10,abs_tol=0): return False
                    if (losing>0 and p['primary']==0) or (declining>0 and p['secondary']==0): return False
                    a=set(q['lossMembers']);b=set(q['declineMembers']);all_members=set(q['included'])
                    left=losing/total;right=declining/total
                    if ((a==all_members and not isclose(left,1,rel_tol=1e-10,abs_tol=0))
                        or (b==all_members and not isclose(right,1,rel_tol=1e-10,abs_tol=0))
                        or (a==b and not isclose(left,right,rel_tol=1e-10,abs_tol=0))
                        or (a<=b and left>right+1e-10) or (b<=a and right>left+1e-10)
                        or (not a&b and left+right>1+1e-10)
                        or (a|b==all_members and left+right<1-1e-10)): return False
                if type(p['sampleSetChanged']) is not bool or p['sampleSetChanged']!=changed: return False
                if (not integer(p['sampleCount']) or not integer(p['candidateCount']) or p['sampleCount']!=q['sampleCount']
                    or p['candidateCount']!=q['candidateCount'] or not close(p['coverage'],q['coverage'])): return False
        if value['algorithmVersion'] in ('enterprise-quarter-v6','enterprise-quarter-v7'):
            from app.macro.enterprise_ledger_contract import valid_ledger
            if not valid_ledger(value.get('ledger'),value): return False
        if 'rea' in value:
            from app.macro.enterprise_rea import valid_rea_projection
            if not valid_rea_projection(value['rea'],value): return False
        if 'industryComparison' in value:
            from app.macro.enterprise_industries import valid_industry_comparison
            if not valid_industry_comparison(value['industryComparison'],value): return False
        if 'officialTargets' in value:
            from app.macro.enterprise_official_targets import valid_official_target_comparison
            if not valid_official_target_comparison(value['officialTargets'],value):return False
        return True
    except (KeyError,TypeError,ValueError,OverflowError,AttributeError):
        return False
