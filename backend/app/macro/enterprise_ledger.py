"""上部账本代理指标；独立可比组，不做经济状态推断或供应商请求。"""
from datetime import date
from dataclasses import asdict
from decimal import Decimal
from fractions import Fraction
from math import isfinite

from app.macro.enterprise import EnterpriseInputs, EnterprisePolicy, _hash, _number, _previous, _select, _top_rows, _visible


def ledger_value(identifier: str, amounts: dict, *, legacy: bool = False) -> tuple[float | None,str]:
    """按公开金额重算；非正经济分母和不可表示比率分别解释。"""
    if identifier=='pricing-power':
        if amounts['revenue']<=0 or amounts['baseRevenue']<=0:
            return None,'non-positive-aggregate-revenue'
        value=(1-amounts['cost']/amounts['revenue'])*100-(1-amounts['baseCost']/amounts['baseRevenue'])*100
        exact=Fraction(Decimal(str(amounts['baseCost'])))*Fraction(Decimal(str(amounts['revenue'])))-Fraction(Decimal(str(amounts['cost'])))*Fraction(Decimal(str(amounts['baseRevenue'])))
        if exact==0 and not legacy:
            value=0.0
    else:
        if amounts['netIncome']<=0:
            return None,'non-positive-aggregate-profit'
        value=amounts['operatingCashflow']/amounts['netIncome']
        exact=Decimal(str(amounts['operatingCashflow']))/Decimal(str(amounts['netIncome']))
    if not isfinite(value) or (exact!=0 and value==0):
        return None,'non-finite-ratio'
    return value,'ready'


def build_ledger(inputs: EnterpriseInputs, as_of: date, policy: EnterprisePolicy, periods: list[date]) -> dict:
    supplemental={r.source_key:r for r in inputs.contract_liabilities or ()} if inputs.orders_enabled else {}
    if inputs.orders_enabled and len(supplemental)!=len(inputs.contract_liabilities or ()):
        raise ValueError('duplicate enterprise order supplement')
    grouped={}
    for r in inputs.financials: grouped.setdefault((r.table,r.symbol,r.end_date),[]).append(r)
    selected={key:_select(tuple(rows),as_of) for key,rows in grouped.items()}
    securities={s.symbol:s for s in inputs.securities}
    industries={}
    for i in inputs.industries: industries.setdefault(i.symbol,[]).append(i)
    def active(symbol,period):
        return [i for i in industries.get(symbol,()) if i.effective_from<=period and (i.effective_to is None or i.effective_to>=period)]
    def root(symbol,period):
        rows=active(symbol,period)
        return rows[0].root_code if len(rows)==1 else None
    metrics=[]
    definitions=[('pricing-power','定价权','百分点'),('cash-conversion','利润变现','倍')]
    if inputs.orders_enabled: definitions.append(('order-temperature','订单温度','%'))
    for identifier,title,unit in definitions:
        pricing=identifier=='pricing-power';points=[];qualities=[];previous=None
        orders=identifier=='order-temperature'
        for period in periods:
            included=[];excluded={};na={};negative=[];zero=[];negative_cost=[];refs={};industry_refs={};security_refs=[]
            zero_contract=[]
            keys=('contractLiabilities','baseContractLiabilities','revenue','baseRevenue') if orders else ('revenue','cost','baseRevenue','baseCost') if pricing else ('operatingCashflow','netIncome')
            totals={key:Decimal(0) for key in keys}
            def remember(r):
                ref={'symbol':r.symbol,'table':r.table,'period':r.end_date.isoformat(),'sourceKey':r.source_key,
                    'visibleAt':_visible(r).isoformat(),'valueHash':_hash(r.values),'compType':r.comp_type,
                    'reportType':r.report_type,'updateFlag':r.update_flag}
                refs[_hash(ref)]=ref
            def remember_industry(symbol,when):
                industry_refs[(symbol,when)]={'symbol':symbol,'evaluatedPeriod':when.isoformat(),
                    'records':[{'rootCode':i.root_code,'effectiveFrom':i.effective_from.isoformat(),
                        'effectiveTo':i.effective_to.isoformat() if i.effective_to else None,'sourceKey':i.source_key}
                        for i in sorted(active(symbol,when),key=lambda i:(str(i.root_code),i.effective_from,str(i.source_key)))]}
            for symbol in sorted(inputs.symbols):
                security=securities.get(symbol)
                security_refs.append({'symbol':symbol,'listDate':str(security.list_date) if security and security.list_date else None})
                remember_industry(symbol,period)
                current=[];conflict=False
                for table in ('income','balance','cashflow'):
                    r,error=selected.get((table,symbol,period),(None,'missing-report'))
                    if error=='revision-conflict':
                        conflict=True
                        for r in _top_rows(tuple(grouped[table,symbol,period]),as_of): remember(r)
                    elif r is not None: current.append(r);remember(r)
                if conflict: excluded[symbol]='revision-conflict';continue
                if security and security.list_date and security.list_date>period: na[symbol]='before-listing';continue
                types={r.comp_type for r in current}
                if len(types)>1: excluded[symbol]='company-type-conflict';continue
                if types & {'2','3','4','7'}: na[symbol]='financial-report';continue
                current_root=root(symbol,period)
                if current_root in ('801780.SI','801790.SI'): na[symbol]='financial-industry-review';continue
                if security is None or security.list_date is None: excluded[symbol]='unknown-listing-date';continue
                if current_root is None: excluded[symbol]='unknown-or-overlapping-industry';continue
                if inputs.industry_root is not None and current_root!=inputs.industry_root:
                    excluded[symbol]='outside-selected-current-industry';continue
                needed=set();reason=None
                def read(table,when,field):
                    nonlocal reason
                    needed.add(when)
                    r,error=selected.get((table,symbol,when),(None,'missing-report'))
                    if r is None:
                        if error=='revision-conflict':
                            for tie in _top_rows(tuple(grouped[table,symbol,when]),as_of): remember(tie)
                        reason=reason or error;return None
                    remember(r)
                    if r.comp_type!='1': reason=reason or 'incompatible-company-type';return None
                    raw=r.values.get(field)
                    if orders and field=='contract_liab' and field not in r.values:
                        evidence=supplemental.get(r.source_key)
                        if evidence is not None:
                            ref={'symbol':symbol,'table':'order-supplement','period':when.isoformat(),
                                'sourceKey':'core.enterprise_order_supplement:'+str(evidence.request_key),
                                'evidenceHash':evidence.evidence_hash,'status':evidence.status,
                                'sourceHash':evidence.source_hash,'availableDate':evidence.available_date.isoformat()}
                            refs[_hash(ref)]=ref
                            if evidence.source_hash!=_hash(asdict(r)): reason=reason or 'supplement-source-mismatch'
                            elif evidence.available_date>as_of: reason=reason or 'supplement-not-visible'
                            elif evidence.status!='matched': reason=reason or 'supplement-'+evidence.status
                            else: raw=evidence.value
                    number=_number(raw)
                    if number is None: reason=reason or 'missing-or-invalid-field'
                    if orders and field=='contract_liab' and number is not None and number<0:
                        reason=reason or 'invalid-negative-contract-liability'
                    return number
                def quarter(table,when,field):
                    value=read(table,when,field)
                    if when.month==3: return value
                    prior=read(table,_previous(when),field)
                    return value-prior if value is not None and prior is not None else None
                if orders:
                    old=period.replace(year=period.year-1)
                    values=dict(zip(keys,(read('balance',period,'contract_liab'),read('balance',old,'contract_liab'),
                        quarter('income',period,'revenue'),quarter('income',old,'revenue'))))
                elif pricing:
                    old=period.replace(year=period.year-1)
                    values=dict(zip(keys,(quarter('income',period,'revenue'),quarter('income',period,'oper_cost'),
                        quarter('income',old,'revenue'),quarter('income',old,'oper_cost'))))
                else:
                    values=dict(zip(keys,(quarter('cashflow',period,'n_cashflow_act'),quarter('income',period,'n_income'))))
                for when in needed: remember_industry(symbol,when)
                if any(when<security.list_date for when in needed): reason=reason or 'pre-listing-input'
                if any(root(symbol,when)!=current_root for when in needed): reason=reason or 'industry-history-missing-or-changed'
                if reason or any(v is None for v in values.values()): excluded[symbol]=reason or 'missing-input';continue
                included.append(symbol)
                for key,value in values.items(): totals[key]+=value
                bases=(values['revenue'],values['baseRevenue']) if pricing or orders else (values['netIncome'],)
                if any(v<0 for v in bases): negative.append(symbol)
                if any(v==0 for v in bases): zero.append(symbol)
                if pricing and (values['cost']<0 or values['baseCost']<0): negative_cost.append(symbol)
                if orders and values['baseContractLiabilities']==0: zero_contract.append(symbol)
            n=len(included);candidates=len(inputs.symbols)-len(na);coverage=n*100/candidates if candidates else 0
            status=('no-candidates' if not candidates else 'insufficient-samples' if n<policy.min_samples
                else 'insufficient-coverage' if n*100<candidates*policy.min_coverage else 'ready')
            if status=='ready':
                decimals={k:v/Decimal(100000000) for k,v in totals.items()}
                amounts={k:float(v) for k,v in decimals.items()}
                if any(not isfinite(amounts[k]) or (v!=0 and (amounts[k]==0 or abs(Decimal.from_float(amounts[k])-v)>abs(v)*Decimal('1e-10'))) for k,v in decimals.items()):
                    status='non-finite-aggregate'
                else:
                    if orders:
                        from app.macro.enterprise_orders import order_values
                        calculated=order_values(amounts)
                    else:
                        value,value_status=ledger_value(identifier,amounts)
                        calculated={'value':value,'status':value_status}
                    points.append({'period':period.isoformat(),'quarter':f'{period.year}Q{period.month//3}',
                        'amounts':amounts,**calculated,'sampleCount':n,'candidateCount':candidates,
                        'coverage':coverage,'sampleSetChanged':previous is not None and previous!=included})
                    previous=list(included)
            qualities.append({'period':period.isoformat(),'status':status,'sampleCount':n,'candidateCount':candidates,
                'coverage':coverage,'included':included,'excluded':excluded,'nonApplicable':na,
                'negativeIndividualBases':negative,'zeroIndividualBases':zero,'negativeQuarterCosts':negative_cost,
                'sourceRefs':sorted(refs.values(),key=lambda r:(r['symbol'],r['table'],r['period'],r['sourceKey'])),
                'industryRefs':[industry_refs[key] for key in sorted(industry_refs)],'securityRefs':security_refs})
            if orders: qualities[-1]['zeroContractLiabilityBases']=zero_contract
        metrics.append({'id':identifier,'title':title,'unit':unit,'points':points,'quality':qualities})
    return {'calculationVersion':'exact-zero-margin-v1','metrics':metrics}
