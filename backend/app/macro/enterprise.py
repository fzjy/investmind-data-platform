"""可比企业样本底座：仅计算现有输入，不做采集、发布或投资状态判断。"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from math import isclose, isfinite

MAX_SAFE_INTEGER = 2**53-1

# 旧卡实际计算输入，不等同于REA投影的两年同比输入。
CARD_QUALITY_FIELDS = {
    'scissors': ('income.n_income_attr_p', 'income.revenue'),
    'inventory': ('balance.inventories', 'income.revenue'),
    'white-strip': ('balance.accounts_receiv', 'income.revenue'),
    'lying-flat': ('cashflow.c_pay_acq_const_fiolta', 'cashflow.n_cashflow_act'),
    'dividend': ('cashflow.c_pay_acq_const_fiolta', 'cashflow.c_pay_dist_dpcp_int_exp'),
    'loss': ('daily_basic.total_mv', 'income.n_income_attr_p'),
}

@dataclass(frozen=True)
class FinancialRecord:
    table: str
    symbol: str
    end_date: date
    ann_date: date | None
    f_ann_date: date | None
    report_type: str | None
    comp_type: str | None
    update_flag: str | None
    values: Mapping[str, object]
    source_key: str


@dataclass(frozen=True)
class SecurityInput:
    symbol: str
    list_date: date | None


@dataclass(frozen=True)
class IndustryInput:
    symbol: str
    root_code: str | None
    effective_from: date
    effective_to: date | None
    source_key: str | None = None


@dataclass(frozen=True)
class IndustryLabelInput:
    code: str
    name: str
    source_key: str


@dataclass(frozen=True)
class ReceivableInput:
    source_key: str
    source_hash: str
    status: str
    value: str | None
    evidence_hash: str
    available_date: date
    request_key: str | None = None


@dataclass(frozen=True)
class MarketValueInput:
    symbol: str
    trade_date: date
    total_mv: object
    source_key: str


@dataclass(frozen=True)
class ReaFieldInput:
    source_key: str
    source_hash: str
    field: str
    status: str
    value: str | None
    evidence_hash: str
    available_date: date
    request_key: str


@dataclass(frozen=True)
class TradingDayInput:
    day: date
    is_open: bool
    source_key: str


@dataclass(frozen=True)
class EnterpriseInputs:
    universe_version: str
    symbols: tuple[str, ...]
    securities: tuple[SecurityInput, ...]
    financials: tuple[FinancialRecord, ...]
    industries: tuple[IndustryInput, ...]
    receivables: tuple[ReceivableInput, ...] | None = None
    cashflow_enabled: bool = False
    distribution_enabled: bool = False
    loss_enabled: bool = False
    market_values: tuple[MarketValueInput, ...] = ()
    trading_days: tuple[TradingDayInput, ...] = ()
    ledger_enabled: bool = False
    contract_liabilities: tuple[ReceivableInput, ...] | None = None
    orders_enabled: bool = False
    rea_fields: tuple[ReaFieldInput, ...] = ()
    industry_labels: tuple[IndustryLabelInput, ...] = ()
    industry_root: str | None = None
    display_periods: tuple[date, ...] | None = None
    renewal_estimation_enabled: bool = False


@dataclass(frozen=True)
class EnterprisePolicy:
    min_coverage: int = 80
    min_samples: int = 5
    max_quarters: int = 40

    def __post_init__(self):
        for value, maximum in ((self.min_coverage,100),(self.min_samples,MAX_SAFE_INTEGER),(self.max_quarters,40)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('invalid enterprise policy')


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _number(value: object) -> Decimal | None:
    if value is None or isinstance(value,bool):
        return None
    try:
        result=Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation,ValueError):
        return None


def _visible(row: FinancialRecord) -> date | None:
    return max((d for d in (row.ann_date,row.f_ann_date) if d is not None),default=None)


def _previous(period: date) -> date:
    return date(period.year,period.month-2,1)-timedelta(days=1)


def _top_rows(rows: tuple[FinancialRecord, ...], cutoff: date) -> tuple[FinancialRecord,...]:
    visible=[r for r in rows if r.report_type=='1' and _visible(r) is not None and _visible(r)<=cutoff and r.end_date<=cutoff
        and getattr(r,'available_date',date.min)<=cutoff]
    if not visible:
        return ()
    def rank(row):
        flag=str(row.update_flag or '')
        return _visible(row),int(flag) if flag.isdigit() else -1
    best=max(map(rank,visible))
    return tuple(r for r in visible if rank(r)==best)


def _select(rows: tuple[FinancialRecord, ...], cutoff: date) -> tuple[FinancialRecord | None,str | None]:
    ties=_top_rows(rows,cutoff)
    if not ties:
        return None,'missing-report'
    if len({_hash({'values':r.values,'compType':r.comp_type}) for r in ties})!=1:
        return None,'revision-conflict'
    return min(ties,key=lambda r:r.source_key),None


def build_enterprise_analysis(inputs: EnterpriseInputs, as_of: date, policy: EnterprisePolicy) -> dict:
    if type(inputs.cashflow_enabled) is not bool:
        raise ValueError('invalid enterprise cashflow switch')
    if type(inputs.distribution_enabled) is not bool or (inputs.distribution_enabled and not inputs.cashflow_enabled):
        raise ValueError('invalid enterprise distribution switch')
    if type(inputs.loss_enabled) is not bool or (inputs.loss_enabled and not inputs.distribution_enabled):
        raise ValueError('invalid enterprise loss switch')
    if type(inputs.ledger_enabled) is not bool or (inputs.ledger_enabled and not inputs.loss_enabled):
        raise ValueError('invalid enterprise ledger switch')
    if type(inputs.orders_enabled) is not bool or (inputs.orders_enabled and not inputs.ledger_enabled):
        raise ValueError('invalid enterprise orders switch')
    symbols=tuple(sorted(inputs.symbols))
    if len(set(symbols))!=len(symbols) or not symbols:
        raise ValueError('invalid enterprise universe')
    for collection in (inputs.securities,inputs.financials,inputs.industries,inputs.market_values):
        if any(r.symbol not in symbols for r in collection):
            raise ValueError('record outside enterprise universe')
    securities={s.symbol:s for s in inputs.securities}
    if len(securities)!=len(inputs.securities):
        raise ValueError('duplicate enterprise security')
    grouped: dict[tuple[str,str,date],list[FinancialRecord]]={}
    for r in inputs.financials:
        if r.table not in ('income','balance','cashflow') or (r.end_date.month,r.end_date.day) not in ((3,31),(6,30),(9,30),(12,31)):
            raise ValueError('invalid enterprise financial identity')
        grouped.setdefault((r.table,r.symbol,r.end_date),[]).append(r)
    selected={key:_select(tuple(rows),as_of) for key,rows in grouped.items()}
    observed=sorted({r.end_date for r in inputs.financials if r.end_date<=as_of and _visible(r) is not None and _visible(r)<=as_of})
    periods=[]
    if observed:
        # 期末当天尚未完整结束，保守只纳入此前已结束的季度。
        current=date(as_of.year,((as_of.month-1)//3)*3+1,1)-timedelta(days=1)
        while current>=observed[0] and len(periods)<policy.max_quarters:
            periods.append(current)
            current=_previous(current)
    periods.reverse()
    if inputs.display_periods is not None:
        periods=list(inputs.display_periods)
        latest=date(as_of.year,((as_of.month-1)//3)*3+1,1)-timedelta(days=1)
        if (periods!=sorted(set(periods)) or len(periods)>policy.max_quarters or any(
                p>latest or (p.month,p.day) not in ((3,31),(6,30),(9,30),(12,31)) for p in periods)):
            raise ValueError('invalid enterprise display periods')

    def active_industries(symbol,period):
        return [i for i in inputs.industries if i.symbol==symbol and i.effective_from<=period and (i.effective_to is None or i.effective_to>=period)]

    def industry(symbol,period):
        active=active_industries(symbol,period)
        return active[0].root_code if len(active)==1 else None

    supplemental={r.source_key:r for r in inputs.receivables or ()}
    if len(supplemental)!=len(inputs.receivables or ()):
        raise ValueError('duplicate enterprise supplement')
    definitions=[
        ('scissors','营收利润剪刀差','n_income_attr_p'),('inventory','宏观库存钟','inventories'),
    ]
    if inputs.receivables is not None or inputs.cashflow_enabled:
        definitions.append(('white-strip','宏观白条指数','accounts_receiv'))
    if inputs.cashflow_enabled:
        definitions.append(('lying-flat','企业躺平指数','c_pay_acq_const_fiolta'))
    if inputs.distribution_enabled:
        definitions.append(('dividend','分红挤出扩产','c_pay_dist_dpcp_int_exp'))
    cards=[]
    for identifier, title, secondary_field in definitions:
        cash_amount = identifier=='lying-flat'
        distribution = identifier=='dividend'
        amount_card = cash_amount or distribution
        points=[]; quality=[]; previous_members=None
        for period in periods:
            old=date(period.year-1,period.month,period.day)
            included=[]; excluded={}; non_applicable={}; refs={}; industry_refs={}; security_refs=[]; negative=[]; zero=[]; totals=[Decimal(0)]*4
            field_coverage=dict.fromkeys(CARD_QUALITY_FIELDS[identifier],0)
            def remember(row):
                ref={'symbol':row.symbol,'table':row.table,'period':row.end_date.isoformat(),'sourceKey':row.source_key,
                    'visibleAt':_visible(row).isoformat(),'valueHash':_hash(row.values),'compType':row.comp_type,
                    'reportType':row.report_type,'updateFlag':row.update_flag}
                refs[_hash(ref)]=ref
            def remember_industry(symbol,when):
                industry_refs[(symbol,when)]={'symbol':symbol,'evaluatedPeriod':when.isoformat(),
                    'records':[{'rootCode':i.root_code,'effectiveFrom':i.effective_from.isoformat(),
                        'effectiveTo':i.effective_to.isoformat() if i.effective_to else None,'sourceKey':i.source_key}
                        for i in sorted(active_industries(symbol,when),key=lambda i:(str(i.root_code),i.effective_from,str(i.source_key)))]}
            for symbol in symbols:
                security=securities.get(symbol)
                security_refs.append({'symbol':symbol,'listDate':security.list_date.isoformat() if security and security.list_date else None,
                    'sourceKey':f'core.security:{symbol}' if security else None})
                remember_industry(symbol,period)
                current_conflict=False
                for table in ('income','balance','cashflow'):
                    row,error=selected.get((table,symbol,period),(None,'missing-report'))
                    if error=='revision-conflict':
                        current_conflict=True
                        for conflict in _top_rows(tuple(grouped[table,symbol,period]),as_of): remember(conflict)
                    elif row is not None:
                        remember(row)
                if current_conflict:
                    excluded[symbol]='revision-conflict';continue
                if security and security.list_date and security.list_date>period:
                    non_applicable[symbol]='before-listing';continue
                root=industry(symbol,period)
                current_types={row.comp_type for table in ('income','balance','cashflow')
                    if (row:=selected.get((table,symbol,period),(None,None))[0]) is not None}
                if len(current_types)>1:
                    excluded[symbol]='company-type-conflict';continue
                if current_types & {'2','3','4','7'}:
                    non_applicable[symbol]='financial-report';continue
                if root in ('801780.SI','801790.SI'):
                    non_applicable[symbol]='financial-industry-review';continue
                if security is None or security.list_date is None:
                    excluded[symbol]='unknown-listing-date';continue
                if root is None:
                    excluded[symbol]='unknown-or-overlapping-industry';continue
                if inputs.industry_root is not None and root!=inputs.industry_root:
                    excluded[symbol]='outside-selected-current-industry';continue
                needed_dates=set(); source_rows={}; reason=None; field_valid={}
                def read(table,when,field):
                    nonlocal reason
                    key=table+'.'+field
                    valid=when>=security.list_date and industry(symbol,when)==root
                    needed_dates.add(when)
                    row,error=selected.get((table,symbol,when),(None,'missing-report'))
                    if row is None:
                        if error=='revision-conflict':
                            for conflict in _top_rows(tuple(grouped[table,symbol,when]),as_of): remember(conflict)
                        reason=reason or error;field_valid[key]=False;return None
                    source_rows[(table,when)]=row
                    if row.comp_type!='1':
                        reason=reason or 'incompatible-company-type';field_valid[key]=False;return None
                    raw=row.values.get(field)
                    if field=='accounts_receiv' and field not in row.values:
                        supplement=supplemental.get(row.source_key)
                        if supplement is not None:
                            ref={'symbol':row.symbol,'table':'receivable-supplement','period':row.end_date.isoformat(),
                                'sourceKey':(f'core.enterprise_receivable_supplement:{supplement.request_key}' if supplement.request_key
                                    else f'enterprise-receivable-evidence:{supplement.evidence_hash}'),
                                'visibleAt':supplement.available_date.isoformat(),'valueHash':supplement.evidence_hash,
                                'compType':row.comp_type,'reportType':row.report_type,'updateFlag':row.update_flag}
                            refs[_hash(ref)]=ref
                            if supplement.source_hash!=_hash(asdict(row)):
                                reason=reason or 'supplement-source-changed'
                                valid=False
                            elif supplement.available_date>as_of:
                                reason=reason or 'supplement-not-yet-visible'
                                valid=False
                            elif supplement.status!='matched':
                                reason=reason or supplement.status
                                valid=False
                            else:
                                raw=supplement.value
                    value=_number(raw)
                    if value is None: reason=reason or 'missing-or-invalid-field'
                    field_valid[key]=field_valid.get(key,True) and valid and value is not None
                    return value
                def quarter(when,field,table='income'):
                    nonlocal reason
                    value=read(table,when,field)
                    payment=field in ('c_pay_acq_const_fiolta','c_pay_dist_dpcp_int_exp')
                    payment_error='negative-capital-expenditure' if field=='c_pay_acq_const_fiolta' else 'negative-distribution-payment'
                    if payment and value is not None and value<0:
                        reason=reason or payment_error
                        field_valid[table+'.'+field]=False
                    if when.month==3: return value
                    prior=read(table,_previous(when),field)
                    if payment and prior is not None and (prior<0 or (value is not None and value<prior)):
                        reason=reason or payment_error
                        field_valid[table+'.'+field]=False
                    return value-prior if value is not None and prior is not None else None
                if distribution:
                    current_revenue=quarter(period,'c_pay_acq_const_fiolta','cashflow')
                    current_secondary=quarter(period,secondary_field,'cashflow')
                    old_revenue=Decimal(0)
                    old_secondary=quarter(old,secondary_field,'cashflow')
                elif cash_amount:
                    current_revenue=quarter(period,'n_cashflow_act','cashflow')
                    current_secondary=quarter(period,secondary_field,'cashflow')
                    old_revenue=old_secondary=Decimal(0)
                else:
                    current_revenue=quarter(period,'revenue'); old_revenue=quarter(old,'revenue')
                    if identifier=='scissors':
                        current_secondary=quarter(period,secondary_field);old_secondary=quarter(old,secondary_field)
                    else:
                        current_secondary=read('balance',period,secondary_field);old_secondary=read('balance',old,secondary_field)
                if any(when<security.list_date for when in needed_dates):
                    reason=reason or 'pre-listing-input'
                if any(industry(symbol,when)!=root for when in needed_dates):
                    reason=reason or 'industry-history-missing-or-changed'
                for when in needed_dates: remember_industry(symbol,when)
                for row in source_rows.values(): remember(row)
                for key,valid in field_valid.items():
                    if valid: field_coverage[key]+=1
                values=((current_secondary,current_revenue,old_secondary,old_revenue) if identifier=='white-strip'
                    else (current_revenue,current_secondary,old_revenue,old_secondary))
                if reason is not None or any(v is None for v in values):
                    excluded[symbol]=reason or 'missing-input';continue
                included.append(symbol)
                totals=[total+value for total,value in zip(totals,values)]
                if distribution:
                    if old_secondary==0: zero.append(symbol)
                elif not cash_amount:
                    if old_revenue<0 or old_secondary<0: negative.append(symbol)
                    if old_revenue==0 or old_secondary==0: zero.append(symbol)
            candidates=len(symbols)-len(non_applicable)
            n=len(included); coverage=float(Decimal(n)*100/candidates) if candidates else 0.0
            status='ready'
            if not candidates: status='no-candidates'
            elif n<policy.min_samples: status='insufficient-samples'
            elif n*100<candidates*policy.min_coverage: status='insufficient-coverage'
            elif not amount_card and (totals[2]<=0 or totals[3]<=0): status='non-positive-aggregate-base'
            if status=='ready':
                if amount_card:
                    primary=totals[0]/Decimal(100000000);secondary=totals[1]/Decimal(100000000)
                else:
                    primary=(totals[0]/totals[2]-1)*100; secondary=(totals[1]/totals[3]-1)*100
                decimals=(primary,secondary,primary-secondary)
                if distribution: decimals=(*decimals,totals[3]/Decimal(100000000))
                values=[float(v) for v in decimals]
                if not all(isfinite(v) for v in values) or (amount_card and (
                    any(d!=0 and (f==0 or abs(Decimal.from_float(f)-d)>abs(d)*Decimal('1e-10'))
                        for d,f in zip(decimals,values))
                    or not isclose(values[2],values[0]-values[1],rel_tol=1e-10,abs_tol=1e-9)
                )):
                    status='non-finite-aggregate'
                else:
                    points.append({'period':period.isoformat(),'quarter':f'{period.year}Q{period.month//3}',
                        'primary':values[0],'secondary':values[1],'result':values[2],'sampleCount':n,
                        'candidateCount':candidates,'coverage':coverage,
                        'sampleSetChanged':previous_members is not None and previous_members!=included})
                    if cash_amount:
                        # 同组金额已汇总并检查转换精度；用公开金额的同一算式，保持跨语言合同一致。
                        ratio=values[1]/values[0]*100 if values[0]>0 else None
                        ratio_status=('non-positive-operating-cashflow' if ratio is None else 'ready' if isfinite(ratio) else 'non-finite-ratio')
                        points[-1].update(capitalExpenditureRatio=ratio if ratio_status=='ready' else None,ratioStatus=ratio_status)
                    if distribution:
                        yoy=(values[1]/values[3]-1)*100 if values[3]>0 else None
                        yoy_status=('zero-distribution-base' if yoy is None else 'ready' if isfinite(yoy) else 'non-finite-yoy')
                        points[-1].update(distributionBase=values[3],distributionYoY=yoy if yoy_status=='ready' else None,
                            distributionYoYStatus=yoy_status)
                    previous_members=list(included)
            quality.append({'period':period.isoformat(),'status':status,'sampleCount':n,'candidateCount':candidates,
                'coverage':coverage,'included':included,'excluded':excluded,'nonApplicable':non_applicable,
                'negativeIndividualBases':negative,'zeroIndividualBases':zero,
                'sourceRefs':sorted(refs.values(),key=lambda ref:(ref['symbol'],ref['table'],ref['period'],ref['sourceKey'])),
                'industryRefs':[industry_refs[key] for key in sorted(industry_refs)],'securityRefs':security_refs,
                'fieldCoverage':field_coverage})
        cards.append({'id':identifier,'title':title,'frequency':'quarterly','primaryUnit':'亿元' if amount_card else '%','secondaryUnit':'亿元' if amount_card else '%',
            'resultUnit':'亿元' if amount_card else '百分点','points':points,'quality':quality})
        if cash_amount: cards[-1]['ratioUnit']='%'
        if distribution: cards[-1]['yoyUnit']='%'
    canonical={
        'poolVersion':inputs.universe_version,'symbols':symbols,
        'securities':sorted((asdict(s) for s in inputs.securities),key=lambda s:s['symbol']),
        'financials':sorted((asdict(r) for r in inputs.financials),key=lambda r:json.dumps(r,sort_keys=True,default=str)),
        'industries':sorted((asdict(i) for i in inputs.industries),key=lambda r:json.dumps(r,sort_keys=True,default=str)),
    }
    if inputs.receivables is not None:
        canonical['receivables']=sorted((asdict(r) for r in inputs.receivables),key=lambda r:r['source_key'])
    if inputs.industry_root is not None:
        canonical['industryRoot']=inputs.industry_root
    if inputs.display_periods is not None:
        canonical['displayPeriods']=inputs.display_periods
    if inputs.cashflow_enabled: canonical['cashflowEnabled']=True
    if inputs.distribution_enabled: canonical['distributionEnabled']=True
    if inputs.loss_enabled:
        from app.macro.enterprise_loss import build_loss_card
        card,dependencies=build_loss_card(inputs,as_of,policy,periods)
        cards.append(card);canonical['lossInputs']=dependencies
    if inputs.ledger_enabled: canonical['ledgerEnabled']=True
    if inputs.orders_enabled:
        canonical['ordersEnabled']=True
        canonical['contractLiabilities']=sorted((asdict(r) for r in inputs.contract_liabilities or ()),key=lambda r:r['source_key'])
    result={'schemaVersion':'1.0','algorithmVersion':'enterprise-quarter-v7' if inputs.orders_enabled else 'enterprise-quarter-v6' if inputs.ledger_enabled else 'enterprise-quarter-v5' if inputs.loss_enabled else 'enterprise-quarter-v4' if inputs.distribution_enabled else 'enterprise-quarter-v3' if inputs.cashflow_enabled else 'enterprise-quarter-v2' if inputs.receivables is not None else 'enterprise-quarter-v1','asOfDate':as_of.isoformat(),
        'poolVersion':inputs.universe_version,'poolCount':len(symbols),'policy':asdict(policy),'inputHash':_hash(canonical),
        'basis':'current-pool-restated-history-not-strict-pit','cards':cards}
    if inputs.ledger_enabled:
        from app.macro.enterprise_ledger import build_ledger
        result['ledger']=build_ledger(inputs,as_of,policy,periods)
    if inputs.industry_root is not None:
        result['industryRoot']=inputs.industry_root
    return result
