"""按固定观察窗口、声明可见财报与同日总市值计算；不提供严格PIT保证。"""
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from math import isclose, isfinite

from app.macro.enterprise import CARD_QUALITY_FIELDS, EnterpriseInputs, EnterprisePolicy, _hash, _number, _previous, _select, _top_rows, _visible


def valuation_anchor(period: date) -> date:
    if period.month==12: return date(period.year+1,4,30)
    return date(period.year,{3:4,6:8,9:10}[period.month],{3:30,6:31,9:31}[period.month])


def valuation_day(period: date,as_of: date,calendar: dict) -> tuple[date | None,str,list]:
    anchor=valuation_anchor(period)
    if as_of<=anchor: return None,'valuation-window-not-due',[]
    day=anchor+timedelta(days=1);used=[]
    while day<=as_of:
        rows=calendar.get(day,[]);used.extend(rows)
        if not rows: return None,'valuation-calendar-missing',used
        if any(type(r.is_open) is not bool for r in rows) or len({r.is_open for r in rows})!=1:
            return None,'valuation-calendar-conflict',used
        if rows[0].is_open:
            return (day,'ready',used) if day<as_of else (None,'valuation-day-not-complete',used)
        day+=timedelta(days=1)
    return None,'valuation-day-not-complete',used


def build_loss_card(inputs: EnterpriseInputs,as_of: date,policy: EnterprisePolicy,periods: list[date]) -> tuple[dict,dict]:
    symbols=sorted(inputs.symbols);securities={s.symbol:s for s in inputs.securities}
    financials={};industries={};calendar={};caps={}
    for row in inputs.financials:
        if row.table=='income': financials.setdefault((row.symbol,row.end_date),[]).append(row)
    for row in inputs.industries: industries.setdefault(row.symbol,[]).append(row)
    for row in inputs.trading_days: calendar.setdefault(row.day,[]).append(row)
    for row in inputs.market_values: caps.setdefault((row.symbol,row.trade_date),[]).append(row)
    def active(symbol,when):
        return [r for r in industries.get(symbol,[]) if r.effective_from<=when and (r.effective_to is None or r.effective_to>=when)]
    def root(symbol,when):
        rows=active(symbol,when)
        return rows[0].root_code if len(rows)==1 else None
    points=[];quality=[];previous=None;used_calendar={};used_days=set()
    for period in periods:
        day,valuation_status,calendar_refs=valuation_day(period,as_of,calendar)
        for row in calendar_refs: used_calendar[_hash(asdict(row))]=asdict(row)
        if day: used_days.add(day)
        cutoff=day or as_of
        # 日期内逐股/期选版；无未来修订倒灌，也不要求无关的资产/现金流字段。
        selected={key:_select(tuple(rows),cutoff) for key,rows in financials.items()}
        included=[];excluded={};na={};negative=[];zero=[];loss_members=[];decline_members=[]
        refs={};industry_refs={};security_refs=[];totals=[Decimal(0)]*3
        field_coverage=dict.fromkeys(CARD_QUALITY_FIELDS['loss'],0)
        for symbol in symbols:
            security=securities.get(symbol)
            security_refs.append({'symbol':symbol,'listDate':str(security.list_date) if security and security.list_date else None})
            def remember_industry(when):
                industry_refs[(symbol,when)]={'symbol':symbol,'evaluatedPeriod':str(when),
                    'records':[{'rootCode':r.root_code,'effectiveFrom':str(r.effective_from),
                        'effectiveTo':str(r.effective_to) if r.effective_to else None,'sourceKey':r.source_key}
                        for r in sorted(active(symbol,when),key=lambda r:str(asdict(r)))]}
            remember_industry(period);current_root=root(symbol,period)
            current,error=selected.get((symbol,period),(None,'missing-report'))
            if security and security.list_date and security.list_date>period:
                na[symbol]='before-listing';continue
            if current and current.comp_type in ('2','3','4','7'):
                na[symbol]='financial-report';continue
            if current_root in ('801780.SI','801790.SI'):
                na[symbol]='financial-industry-review';continue
            if security is None or security.list_date is None:
                excluded[symbol]='unknown-listing-date';continue
            if current_root is None:
                excluded[symbol]='unknown-or-overlapping-industry';continue
            if inputs.industry_root is not None and current_root!=inputs.industry_root:
                excluded[symbol]='outside-selected-current-industry';continue
            if day is None:
                excluded[symbol]=valuation_status;continue
            needed=set();reason=None;profit_valid=True
            def read(when):
                nonlocal reason,profit_valid
                needed.add(when)
                row,error=selected.get((symbol,when),(None,'missing-report'))
                evidence=(row,) if row else _top_rows(tuple(financials.get((symbol,when),[])),cutoff)
                for item in evidence:
                    ref={'symbol':symbol,'table':'income','period':str(when),'sourceKey':item.source_key,
                        'visibleAt':str(_visible(item)),'valueHash':_hash(item.values),'compType':item.comp_type,
                        'reportType':item.report_type,'updateFlag':item.update_flag}
                    refs[_hash(ref)]=ref
                if row is None:
                    reason=reason or error;profit_valid=False;return None
                if row.comp_type!='1': reason=reason or 'incompatible-company-type'
                value=_number(row.values.get('n_income_attr_p'))
                if value is None: reason=reason or 'missing-or-invalid-field'
                profit_valid=(profit_valid and row.comp_type=='1' and value is not None
                    and when>=security.list_date and root(symbol,when)==current_root)
                return value
            def quarter(when):
                current=read(when)
                if when.month==3: return current
                prior=read(_previous(when))
                return current-prior if current is not None and prior is not None else None
            profit=quarter(period);old_profit=quarter(date(period.year-1,period.month,period.day))
            if any(when<security.list_date for when in needed): reason=reason or 'pre-listing-input'
            for when in (*sorted(needed),day): remember_industry(when)
            if any(root(symbol,when)!=current_root for when in (*needed,day)):
                reason=reason or 'industry-history-missing-or-changed'
            market_rows=caps.get((symbol,day),[])
            for row in market_rows:
                ref={'symbol':symbol,'table':'daily_basic','period':str(day),'sourceKey':row.source_key,'valueHash':_hash(row.total_mv)}
                refs[_hash(ref)]=ref
            values=[_number(row.total_mv) for row in market_rows]
            if not values: reason=reason or 'missing-market-value'
            elif any(v is None or v<=0 for v in values): reason=reason or 'invalid-market-value'
            elif len(set(values))!=1: reason=reason or 'market-value-conflict'
            if profit_valid: field_coverage['income.n_income_attr_p']+=1
            if values and all(v is not None and v>0 for v in values) and len(set(values))==1 and root(symbol,day)==current_root:
                field_coverage['daily_basic.total_mv']+=1
            if reason is not None:
                excluded[symbol]=reason;continue
            cap=values[0];included.append(symbol);totals[0]+=cap
            if profit<0: totals[1]+=cap;loss_members.append(symbol)
            if profit<old_profit: totals[2]+=cap;decline_members.append(symbol)
            if old_profit<0: negative.append(symbol)
            if old_profit==0: zero.append(symbol)
        n=len(included);candidates=len(symbols)-len(na);coverage=n*100/candidates if candidates else 0.0
        status=('no-candidates' if not candidates else 'insufficient-samples' if n<policy.min_samples
            else 'insufficient-coverage' if n*100<candidates*policy.min_coverage else 'ready')
        if status=='ready':
            amounts=[v/10000 for v in totals]  # 源万元→公开亿元。
            public_amounts=[float(v) for v in amounts]
            if (not all(isfinite(v) for v in public_amounts) or any(d!=0 and (f==0 or abs(Decimal.from_float(f)-d)>abs(d)*Decimal('1e-10'))
                for d,f in zip(amounts,public_amounts))): status='non-finite-aggregate'
            else:
                primary=public_amounts[1]/public_amounts[0]*100;secondary=public_amounts[2]/public_amounts[0]*100
                exact=(totals[1]/totals[0]*100,totals[2]/totals[0]*100)
                if any(d!=0 and (f==0 or not isclose(f,float(d),rel_tol=1e-10,abs_tol=0)) for d,f in zip(exact,(primary,secondary))):
                    status='non-finite-aggregate'
                else:
                    points.append({'period':str(period),'quarter':f'{period.year}Q{period.month//3}',
                        'primary':primary,'secondary':secondary,'result':primary-secondary,'sampleCount':n,
                        'candidateCount':candidates,'coverage':coverage,'sampleSetChanged':previous is not None and previous!=included,
                        'valuationDate':str(day),'totalMarketValue':public_amounts[0],
                        'lossMarketValue':public_amounts[1],'declineMarketValue':public_amounts[2]})
                    previous=list(included)
        quality.append({'period':str(period),'status':status,'sampleCount':n,'candidateCount':candidates,'coverage':coverage,
            'included':included,'excluded':excluded,'nonApplicable':na,'negativeIndividualBases':negative,'zeroIndividualBases':zero,
            'valuationAnchor':str(valuation_anchor(period)),'valuationDate':str(day) if day else None,'valuationStatus':valuation_status,
            'lossMembers':loss_members,'declineMembers':decline_members,'sourceRefs':sorted(refs.values(),key=lambda r:(r['symbol'],r['table'],r['period'],r['sourceKey'])),
            'industryRefs':[industry_refs[key] for key in sorted(industry_refs)],'securityRefs':security_refs,
            'valuationRefs':[{'day':str(r.day),'isOpen':r.is_open,'sourceKey':r.source_key} for r in calendar_refs],
            'fieldCoverage':field_coverage})
    dependencies={'calendar':sorted(used_calendar.values(),key=lambda r:(r['day'],r['source_key'])),
        'marketValues':sorted((asdict(r) for r in inputs.market_values if r.trade_date in used_days),key=lambda r:(r['symbol'],r['trade_date'],r['source_key']))}
    return {'id':'loss','title':'亏损扩散度','frequency':'quarterly','primaryUnit':'%','secondaryUnit':'%',
        'resultUnit':'百分点','marketValueUnit':'亿元','points':points,'quality':quality},dependencies
