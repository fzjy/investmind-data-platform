"""当前配置池内行业分析；只复用合格输入，不扩大范围或猜权重。"""
from dataclasses import asdict, replace
from datetime import date

from app.macro.enterprise import _hash

VERSION='enterprise-industry-v1'
WEIGHTING='amount-aggregate-not-market-cap-weighted'


def parent_periods(parent):
    return sorted({q['period'] for card in parent['cards'] for q in card['quality']})


def classification_identity(value):
    return {'snapshotDate':value['snapshotDate'],'unassigned':value['unassigned'],
        'groups':[{key:g[key] for key in ('code','name','labelSource','classificationSource','symbols')} for g in value['groups']]}


def subset_enterprise_inputs(inputs,symbols,periods,*,industry_root=None):
    """成员及所有股票级财务/补充/估值同范围；交易日历与公共标签可复用。"""
    members=set(symbols)
    if not members or not members<=set(inputs.symbols):raise ValueError('ENTERPRISE_SUBSET_INVALID')
    records=tuple(r for r in inputs.financials if r.symbol in members);keys={r.source_key for r in records}
    return replace(inputs,symbols=tuple(sorted(members)),financials=records,
        securities=tuple(s for s in inputs.securities if s.symbol in members),
        industries=tuple(i for i in inputs.industries if i.symbol in members),
        industry_root=inputs.industry_root if industry_root is None else industry_root,
        display_periods=tuple(date.fromisoformat(p) for p in periods),
        receivables=None if inputs.receivables is None else tuple(e for e in inputs.receivables if e.source_key in keys),
        contract_liabilities=None if inputs.contract_liabilities is None else tuple(e for e in inputs.contract_liabilities if e.source_key in keys),
        rea_fields=tuple(e for e in inputs.rea_fields if e.source_key in keys),
        market_values=tuple(v for v in inputs.market_values if v.symbol in members))


def build_industry_comparison(inputs,as_of,policy,parent):
    from app.macro.enterprise_contract import public_enterprise_analysis
    labels={label.code:label for label in inputs.industry_labels}
    if len(labels)!=len(inputs.industry_labels) or any(not l.code or not l.name.strip() or not l.source_key for l in labels.values()):
        raise ValueError('INDUSTRY_LABEL_INVALID')
    groups={};unassigned={}
    for symbol in sorted(inputs.symbols):
        matches=[i for i in inputs.industries if i.symbol==symbol and i.effective_from<=as_of
            and (i.effective_to is None or i.effective_to>=as_of)]
        if len(matches)!=1 or not matches[0].root_code:
            unassigned[symbol]='unknown-or-overlapping-current-industry'
        else: groups.setdefault(matches[0].root_code,[]).append(symbol)
    result={'version':VERSION,'sourceHash':parent['inputHash'],'snapshotDate':as_of.isoformat(),
        'basis':'current-pool-current-industry-restated-history-not-strict-pit','provider':'tushare','classification':'SW2021',
        'weightingMethod':WEIGHTING,'lossWeightingMethod':'same-valuation-day-total-market-cap',
        'flowMetricMode':'single-quarter-from-cumulative','groups':[],'unassigned':unassigned}
    for code,symbols in sorted(groups.items()):
        label=labels.get(code)
        child=subset_enterprise_inputs(inputs,symbols,parent_periods(parent),industry_root=code)
        result['groups'].append({'code':code,'name':label.name if label else code,
            'labelSource':'classification' if label else 'code-fallback',
            'classificationSource':asdict(label) if label else None,'symbols':symbols,
            'analysis':public_enterprise_analysis(child,as_of,policy,rea_enabled=True)})
    result['classificationHash']=_hash(classification_identity(result))
    return result


def valid_industry_comparison(value,parent):
    from app.macro.enterprise_contract import valid_enterprise_analysis
    from app.macro.enterprise_rea import valid_rea_projection, same_rea_mode
    try:
        if (not isinstance(value,dict) or value['version']!=VERSION or value['sourceHash']!=parent['inputHash']
                or value['snapshotDate']!=parent['asOfDate'] or value['basis']!='current-pool-current-industry-restated-history-not-strict-pit'
                or value['provider']!='tushare' or value['classification']!='SW2021' or value['weightingMethod']!=WEIGHTING
                or value['lossWeightingMethod']!='same-valuation-day-total-market-cap' or value['flowMetricMode']!='single-quarter-from-cumulative'
                or value['classificationHash']!=_hash(classification_identity(value))): return False
        groups=value['groups'];unassigned=value['unassigned']
        if not isinstance(groups,list) or not isinstance(unassigned,dict) or not all(isinstance(v,str) and v for v in unassigned.values()): return False
        codes=[g['code'] for g in groups]
        if codes!=sorted(set(codes)) or any(not isinstance(c,str) or not c for c in codes): return False
        members=list(unassigned)
        for group in groups:
            analysis=group['analysis'];symbols=group['symbols'];label=group['classificationSource']
            if (not isinstance(group['name'],str) or not group['name'] or not isinstance(symbols,list)
                    or symbols!=sorted(set(symbols)) or not symbols or symbols!=analysis['symbols']
                    or 'industryComparison' in analysis or analysis.get('industryRoot')!=group['code'] or analysis['policy']!=parent['policy']
                    or analysis['asOfDate']!=parent['asOfDate'] or analysis['poolVersion']!=parent['poolVersion']
                    or analysis['algorithmVersion']!=parent['algorithmVersion'] or not same_rea_mode(analysis,parent)
                    or not valid_enterprise_analysis(analysis) or not valid_rea_projection(analysis['rea'],analysis)): return False
            if any([q['period'] for q in metric['quality']]!=parent_periods(parent)
                    for metric in analysis['cards']+analysis['rea']['series']): return False
            if group['labelSource']=='classification':
                if not isinstance(label,dict) or label['code']!=group['code'] or label['name']!=group['name'] or not label['source_key']: return False
            elif group['labelSource']!='code-fallback' or label is not None or group['name']!=group['code']: return False
            members.extend(symbols)
        return len(members)==len(set(members))==parent['poolCount'] and set(members)==set(parent['symbols'])
    except (KeyError,TypeError,ValueError,AttributeError): return False
