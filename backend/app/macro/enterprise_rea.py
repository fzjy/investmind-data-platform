"""REA展示投影：独立同组同比，不改旧算法、Core或已发布历史。"""
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from math import isclose, isfinite
from fractions import Fraction
import re

from app.macro.enterprise import _hash, _number, _previous, _select, _visible, EnterpriseInputs, EnterprisePolicy
from app.macro.rea_input_preview import NODE_IDS, SOURCE_FIELDS
from app.macro.renewal_estimation import FIELDS, MODE, choose_depreciation, missing_observation, evidence_hash, valid_evidence

# a/b是详情两线的金额，oldA/oldB是同组上年同季。单位为源币金额元。
DEFINITIONS = {
    'order': ('%', ('balance', 'contract_liab'), ('income', 'revenue')),
    'pricing': ('pct', ('income', 'gross_profit'), ('income', 'revenue')),
    'cash': ('x', ('cashflow', 'n_cashflow_act'), ('income', 'n_income')),
    'capacity': ('pct', ('income', 'revenue'), ('balance', 'fix_assets')),
    'renewal': ('x', ('cashflow', 'c_pay_acq_const_fiolta'), ('cashflow', 'depreciation')),
    'employee': ('pct', ('cashflow', 'c_paid_to_for_empl'), ('income', 'revenue')),
    'payable': ('pct', ('balance', 'acct_payable'), ('income', 'revenue')),
    'debt': ('pct', ('balance', 'money_cap'), ('balance', 'interest_debt')),
    'lying-flat': ('pct', ('cashflow', 'c_pay_acq_const_fiolta'), ('cashflow', 'n_cashflow_act')),
    'dividend': ('pct', ('cashflow', 'c_pay_acq_const_fiolta'), ('cashflow', 'c_pay_dist_dpcp_int_exp')),
}


def rea_values(identifier: str, amounts: dict) -> dict:
    a,b,old_a,old_b=(amounts[k] if isinstance(amounts[k],Fraction) else Fraction(Decimal(str(amounts[k]))) for k in ('a','b','oldA','oldB'))
    def finite(value):
        if value is None: return None
        try: number=float(value)
        except OverflowError: return None
        return number if isfinite(number) and not (number==0 and value!=0) else None
    primary=(a/old_a-1)*100 if old_a>0 else None
    secondary=(b/old_b-1)*100 if old_b>0 else None
    spread=primary-secondary if primary is not None and secondary is not None else None
    ratio=a/b if b>0 else None
    if identifier=='pricing': value=(a/b-old_a/old_b)*100 if b>0 and old_b>0 else None
    elif identifier=='capacity': value=(a/b-old_a/old_b)*100 if b>0 and old_b>0 else None
    elif identifier in ('cash','renewal'): value=ratio
    elif identifier=='order': value=primary
    elif identifier in ('lying-flat','dividend'): value=-spread if spread is not None else None
    else: value=spread
    return {'value':finite(value),'primary':finite(primary),'secondary':finite(secondary),'spread':finite(spread),
        'ratio':finite(ratio), 'status':'ready' if finite(value) is not None else
        'non-positive-yoy-base' if old_a<=0 or old_b<=0 else 'non-positive-denominator' if b<=0 else 'non-finite-result'}


def _equal(a,b):
    return a is None and b is None or (type(a) in (int,float) and type(b) in (int,float)
        and isfinite(a) and isfinite(b) and (a==0)==(b==0) and isclose(a,b,rel_tol=1e-10,abs_tol=0))


def _valid_amounts(identifier,amounts):
    nonnegative={'order':('a','oldA'),'capacity':('b','oldB'),'renewal':('a','b','oldA','oldB'),
        'employee':('a','oldA'),'payable':('a','oldA'),'debt':('a','b','oldA','oldB'),
        'lying-flat':('a','oldA'),'dividend':('a','b','oldA','oldB')}
    return all(amounts[k]>=0 for k in nonnegative.get(identifier,()))


def rea_required_fields(identifier):
    fields=set()
    for table,field in DEFINITIONS[identifier][1:]:
        parts=('revenue','oper_cost') if field=='gross_profit' else ('depr_fa_coga_dpba','amort_intang_assets','lt_amort_deferred_exp') if field=='depreciation' else ('st_borr','lt_borr','bond_payable','non_cur_liab_due_1y') if field=='interest_debt' else (field,)
        fields.update(table+'.'+part for part in parts)
    return sorted(fields)


def build_rea_projection(inputs: EnterpriseInputs, as_of: date, policy: EnterprisePolicy, legacy: dict) -> dict:
    grouped={}
    for row in inputs.financials: grouped.setdefault((row.table,row.symbol,row.end_date),[]).append(row)
    selected={key:_select(tuple(rows),as_of) for key,rows in grouped.items()}
    securities={s.symbol:s for s in inputs.securities}
    supplements={s.source_key:s for s in inputs.contract_liabilities or ()}
    if len(supplements)!=len(inputs.contract_liabilities or ()): raise ValueError('duplicate order supplement')
    rea_fields={(s.source_key,s.field):s for s in inputs.rea_fields}
    if len(rea_fields)!=len(inputs.rea_fields): raise ValueError('duplicate REA field supplement')
    def roots(symbol,when):
        return [i.root_code for i in inputs.industries if i.symbol==symbol and i.effective_from<=when
            and (i.effective_to is None or i.effective_to>=when)]
    periods=[]
    observed=[when for (_,_,when),(row,_) in selected.items() if row is not None]
    current=date(as_of.year,((as_of.month-1)//3)*3+1,1)-timedelta(days=1)
    while observed and current>=min(observed) and len(periods)<policy.max_quarters:
        periods.append(current);current=_previous(current)
    periods.reverse()
    if inputs.display_periods is not None: periods=list(inputs.display_periods)
    series=[]
    for identifier,(unit,first,second) in DEFINITIONS.items():
        points=[];qualities=[];previous=None
        for period in periods:
            totals={k:Fraction(0) for k in ('a','b','oldA','oldB')}
            included=[];excluded={};na={};refs=[];depreciation_members=[]
            field_coverage=dict.fromkeys(rea_required_fields(identifier),0)
            for symbol in sorted(inputs.symbols):
                security=securities.get(symbol)
                current_rows=[selected.get((table,symbol,period),(None,None)) for table in ('income','balance','cashflow')]
                types={r.comp_type for r,_ in current_rows if r is not None}
                root=roots(symbol,period)
                if security and security.list_date and security.list_date>period: na[symbol]='before-listing';continue
                if types & {'2','3','4','7'} or root in (['801780.SI'],['801790.SI']): na[symbol]='financial-report';continue
                if any(error=='revision-conflict' for _,error in current_rows): excluded[symbol]='revision-conflict';continue
                if len(types)>1: excluded[symbol]='company-type-conflict';continue
                if not security or not security.list_date: excluded[symbol]='unknown-listing-date';continue
                if len(root)!=1 or root[0] is None: excluded[symbol]='unknown-or-overlapping-industry';continue
                if inputs.industry_root is not None and root!=[inputs.industry_root]:
                    excluded[symbol]='outside-selected-current-industry';continue
                reason=None;source={};field_valid={};decisions={}
                def depreciation(when):
                    nonlocal reason
                    def observation(period_text):
                        day=date.fromisoformat(period_text)
                        row,error=selected.get(('cashflow',symbol,day),(None,'missing-report'))
                        result=missing_observation(period_text)
                        if row is None:
                            result['reason']=error or 'missing-report'
                            if result['reason']=='missing-report' and any(_visible(r) is not None and _visible(r)>as_of
                                    for r in grouped.get(('cashflow',symbol,day),())):
                                result['reason']='depreciation-source-not-visible'
                            return result
                        row_hash=_hash(asdict(row));source[row.source_key]=row_hash
                        available=max(d for d in (row.ann_date,row.f_ann_date) if d is not None)
                        result.update(symbol=row.symbol,sourceKey=row.source_key,sourceHash=row_hash,availableDate=available.isoformat(),reason=None)
                        problems=[]
                        if day<security.list_date:problems.append('pre-listing-input')
                        if roots(symbol,day)!=root:problems.append('industry-history-missing-or-changed')
                        if row.comp_type!='1':problems.append('incompatible-company-type')
                        for field in FIELDS:
                            raw=row.values.get(field)
                            if field not in row.values:
                                supplement=rea_fields.get((row.source_key,field))
                                if supplement is not None:
                                    source['rea-supplement:'+supplement.request_key+':'+field]=supplement.evidence_hash
                                    available=max(available,supplement.available_date)
                                    if supplement.source_hash!=row_hash:problems.append('rea-supplement-source-mismatch')
                                    elif supplement.available_date>as_of:problems.append('rea-supplement-not-visible')
                                    elif supplement.status!='matched':
                                        if supplement.status not in ('returned-null','missing-field'):problems.append('rea-supplement-'+supplement.status)
                                    else:raw=supplement.value
                            number=_number(raw)
                            if raw is not None and (number is None or number<0):problems.append('invalid-depreciation-field:'+field)
                            if number is not None and number>=0:result['fields'][field]=format(number,'f')
                        result['availableDate']=available.isoformat()
                        result['reason']=problems[0] if problems else None
                        return result
                    decision=choose_depreciation(when,observation)
                    decisions['current' if when==period else 'yearAgo']=decision
                    good=decision['value'] is not None
                    for field in FIELDS:
                        key='cashflow.'+field;field_valid[key]=field_valid.get(key,True) and good
                    if not good:
                        reason=reason or decision['reason'];return None
                    v=decision['value'];return Fraction(int(v['numerator']),int(v['denominator']))
                def read(table,when,field):
                    nonlocal reason
                    key=table+'.'+field
                    valid=when>=security.list_date and roots(symbol,when)==root
                    if when<security.list_date: reason=reason or 'pre-listing-input'
                    if roots(symbol,when)!=root: reason=reason or 'industry-history-missing-or-changed'
                    row,error=selected.get((table,symbol,when),(None,'missing-report'))
                    if row is None: reason=reason or error;field_valid[key]=False;return None
                    source[row.source_key]=_hash(asdict(row))
                    if row.comp_type!='1': reason=reason or 'incompatible-company-type';field_valid[key]=False;return None
                    raw=row.values.get(field)
                    if identifier in NODE_IDS and field in SOURCE_FIELDS.get(table,()) and field not in row.values:
                        supplement=rea_fields.get((row.source_key,field))
                        if supplement is not None:
                            source['rea-supplement:'+supplement.request_key+':'+field]=supplement.evidence_hash
                            if supplement.source_hash!=_hash(asdict(row)): reason=reason or 'rea-supplement-source-mismatch'
                            elif supplement.available_date>as_of: reason=reason or 'rea-supplement-not-visible'
                            elif supplement.status!='matched': reason=reason or 'rea-supplement-'+supplement.status
                            else: raw=supplement.value
                    if field=='contract_liab' and field not in row.values:
                        supplement=supplements.get(row.source_key)
                        if supplement is not None:
                            source['supplement:'+str(supplement.request_key)]=supplement.evidence_hash
                            if supplement.source_hash!=_hash(asdict(row)): reason=reason or 'supplement-source-mismatch'
                            elif supplement.available_date>as_of: reason=reason or 'supplement-not-visible'
                            elif supplement.status!='matched': reason=reason or 'supplement-'+supplement.status
                            else: raw=supplement.value
                    value=_number(raw)
                    if value is None: reason=reason or 'missing-field:'+field
                    # 净流量/成本/利润允许负值；支付额和存量余额不允许负值。
                    if value is not None and value<0 and (table=='balance' or field.startswith(('c_pay_','c_paid_')) or field in ('depr_fa_coga_dpba','amort_intang_assets','lt_amort_deferred_exp')):
                        reason=reason or 'negative-field:'+field
                        valid=False
                    field_valid[key]=field_valid.get(key,True) and valid and value is not None
                    return Fraction(value) if value is not None else None
                def fact(table,when,field):
                    if identifier=='renewal' and inputs.renewal_estimation_enabled and field=='depreciation':
                        return depreciation(when)
                    if field=='gross_profit':
                        revenue=fact('income',when,'revenue');cost=fact('income',when,'oper_cost')
                        return revenue-cost if revenue is not None and cost is not None else None
                    if field in ('depreciation','interest_debt'):
                        fields=('depr_fa_coga_dpba','amort_intang_assets','lt_amort_deferred_exp') if field=='depreciation' else ('st_borr','lt_borr','bond_payable','non_cur_liab_due_1y')
                        values=[fact(table,when,f) for f in fields]
                        return sum(values) if all(v is not None for v in values) else None
                    value=read(table,when,field)
                    if table!='balance' and when.month!=3:
                        prior=read(table,_previous(when),field)
                        value=value-prior if value is not None and prior is not None else None
                        if value is not None and value<0 and (field.startswith(('c_pay_','c_paid_')) or field in ('depr_fa_coga_dpba','amort_intang_assets','lt_amort_deferred_exp')):
                            nonlocal reason
                            reason=reason or 'negative-quarter-payment:'+field
                            field_valid[table+'.'+field]=False
                    return value
                old=period.replace(year=period.year-1)
                values=dict(zip(totals,(fact(first[0],period,first[1]),fact(second[0],period,second[1]),
                    fact(first[0],old,first[1]),fact(second[0],old,second[1]))))
                refs.append({'symbol':symbol,'sources':source})
                for key,valid in field_valid.items():
                    if valid: field_coverage[key]+=1
                if reason or any(v is None for v in values.values()): excluded[symbol]=reason or 'missing-input';continue
                included.append(symbol)
                if identifier=='renewal' and inputs.renewal_estimation_enabled:
                    depreciation_members.append({'symbol':symbol,**decisions})
                for key,value in values.items(): totals[key]+=value
            n=len(included);candidates=len(inputs.symbols)-len(na);coverage=n*100/candidates if candidates else 0
            status='no-candidates' if not candidates else 'insufficient-samples' if n<policy.min_samples else 'insufficient-coverage' if coverage<policy.min_coverage else 'ready'
            if status=='ready':
                try: amounts={k:float(v) for k,v in totals.items()}
                except OverflowError: amounts={k:float('inf') for k in totals}
                if any(not isfinite(v) or (v==0 and totals[k]!=0) for k,v in amounts.items()): status='non-finite-aggregate'
                else:
                    exact=rea_values(identifier,totals);public=rea_values(identifier,amounts)
                    if exact['status']!=public['status'] or not all(_equal(exact[k],public[k]) for k in ('value','primary','secondary','spread','ratio')):
                        status='non-finite-aggregate'
                    else:
                        points.append({'period':period.isoformat(),'amounts':amounts,**exact,
                            'sampleCount':n,'candidateCount':candidates,'coverage':coverage,'sampleSetChanged':previous is not None and previous!=included})
                        previous=list(included)
            qualities.append({'period':period.isoformat(),'status':status,'sampleCount':n,'candidateCount':candidates,
                'coverage':coverage,'included':included,'excluded':excluded,'nonApplicable':na,'provenanceHash':_hash(refs),
                'sourceRefCount':sum(len(r['sources']) for r in refs),'fieldCoverage':field_coverage})
            if identifier=='renewal' and inputs.renewal_estimation_enabled:
                qualities[-1]['depreciationEvidence']={'version':'renewal-depreciation-v1','members':depreciation_members,
                    'estimatedMembers':[m['symbol'] for m in depreciation_members if m['current']['estimated'] or m['yearAgo']['estimated']],
                    'evidenceHash':evidence_hash(period.isoformat(),depreciation_members)}
        series.append({'id':identifier,'unit':unit,'points':points,'quality':qualities})
    return {'version':'macro-enterprise-rea-v2' if inputs.renewal_estimation_enabled else 'macro-enterprise-rea-v1','sourceHash':legacy['inputHash'],'poolCount':len(inputs.symbols),'series':series,
        'depreciationMode':MODE if inputs.renewal_estimation_enabled else 'strict-single-quarter-three-components-no-estimates',
        'debtMode':'short-long-bonds-current-noncurrent-proxy'}


def same_rea_mode(child, parent):
    source=parent.get('rea');target=child.get('rea')
    if source is None:return isinstance(target,dict) and target.get('version')=='macro-enterprise-rea-v1'
    return isinstance(source,dict) and isinstance(target,dict) and all(source.get(k)==target.get(k)
        for k in ('version','depreciationMode','debtMode'))


def valid_rea_projection(value: object, parent: dict) -> bool:
    def numeric(v): return type(v) in (int,float) and isfinite(v)
    def equal(a,b): return _equal(a,b)
    def quarter(v):
        d=date.fromisoformat(v)
        return d.isoformat()==v and (d.month,d.day) in ((3,31),(6,30),(9,30),(12,31)) and d<date.fromisoformat(parent['asOfDate'])
    try:
        if (not isinstance(value,dict) or value['version'] not in ('macro-enterprise-rea-v1','macro-enterprise-rea-v2')
            or value['sourceHash']!=parent['inputHash'] or value['poolCount']!=parent['poolCount']
            or value['depreciationMode']!=(MODE if value['version']=='macro-enterprise-rea-v2' else 'strict-single-quarter-three-components-no-estimates')
            or value['debtMode']!='short-long-bonds-current-noncurrent-proxy'
            or [m['id'] for m in value['series']]!=list(DEFINITIONS)): return False
        universe=set(parent['symbols']);policy=parent['policy']
        for m in value['series']:
            if m['unit']!=DEFINITIONS[m['id']][0]: return False
            qualities=m['quality'];points=m['points']
            if len(qualities)>policy['max_quarters']: return False
            for collection in (qualities,points):
                periods=[p['period'] for p in collection]
                if periods!=sorted(set(periods)) or not all(quarter(p) for p in periods): return False
            qmap={q['period']:q for q in qualities}
            for q in qualities:
                inc=q['included'];exc=q['excluded'];na=q['nonApplicable']
                renewal_estimates=value['version']=='macro-enterprise-rea-v2' and m['id']=='renewal'
                if renewal_estimates:
                    if not valid_evidence(q.get('depreciationEvidence'),q['period'],inc,date.fromisoformat(parent['asOfDate'])):return False
                elif 'depreciationEvidence' in q:return False
                if not isinstance(inc,list) or inc!=sorted(set(inc)) or not isinstance(exc,dict) or not isinstance(na,dict): return False
                if (set(inc)|set(exc)|set(na)!=universe or set(inc)&set(exc) or set(inc)&set(na) or set(exc)&set(na)): return False
                n=len(inc);c=len(universe)-len(na);coverage=n*100/c if c else 0
                expected='no-candidates' if not c else 'insufficient-samples' if n<policy['min_samples'] else 'insufficient-coverage' if coverage<policy['min_coverage'] else 'ready'
                if q['status'] not in (expected,'non-finite-aggregate' if expected=='ready' else expected): return False
                if type(q['sampleCount']) is not int or type(q['candidateCount']) is not int or q['sampleCount']!=n or q['candidateCount']!=c or not equal(q['coverage'],coverage): return False
                if not isinstance(q['provenanceHash'],str) or not re.fullmatch('[0-9a-f]{64}',q['provenanceHash']): return False
                if type(q['sourceRefCount']) is not int or not 0<=q['sourceRefCount']<=2**53-1: return False
                if 'fieldCoverage' in q:
                    fields=q['fieldCoverage']
                    if (not isinstance(fields,dict) or sorted(fields)!=rea_required_fields(m['id'])
                            or any(type(v) is not int or not n<=v<=c for v in fields.values())): return False
                if not all(isinstance(s,str) and s for s in [*exc.values(),*na.values()]): return False
            if {p['period'] for p in points}!={q['period'] for q in qualities if q['status']=='ready'}: return False
            previous=None
            for p in points:
                q=qmap[p['period']];a=p['amounts']
                if value['version']=='macro-enterprise-rea-v2' and m['id']=='renewal':
                    if not valid_evidence(q.get('depreciationEvidence'),q['period'],q['included'],date.fromisoformat(parent['asOfDate']),a):return False
                if set(a)!= {'a','b','oldA','oldB'} or not all(numeric(v) for v in a.values()): return False
                if not _valid_amounts(m['id'],a): return False
                expected=rea_values(m['id'],a)
                if p['status']!=expected['status'] or not all(equal(p[k],expected[k]) for k in ('value','primary','secondary','spread','ratio')): return False
                if any(p[k]!=q[k] for k in ('sampleCount','candidateCount','coverage')): return False
                if type(p['sampleSetChanged']) is not bool or p['sampleSetChanged']!=(previous is not None and previous!=q['included']): return False
                previous=q['included']
        return True
    except (KeyError,TypeError,ValueError,OverflowError,ArithmeticError):
        return False
