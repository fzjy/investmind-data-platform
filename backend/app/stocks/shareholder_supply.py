"""股东回报的只读投影：已观察事实不等同完整财年或完整回购方案。"""
from collections import defaultdict
from datetime import date
from sqlalchemy import select
from app.db.models import ShareholderCount,Repurchase,TradeCalendar
from app.stocks.workbench_enrichment import number


def series(label,unit,points,color='#0ea5e9'):
    return dict(label=label,unit=unit,color=color,points=points)


def concentration(rows,basics,as_of):
    groups=defaultdict(list)
    for r in rows:
        if r.ann_date and r.ann_date<=as_of and r.end_date<=as_of:groups[r.end_date].append(r)
    counts=[];amounts=[]
    for period,items in sorted(groups.items()):
        latest=max(r.ann_date for r in items)
        values={number(r.holder_num) for r in items if r.ann_date==latest}
        if len(values)!=1 or None in values:continue
        count=next(iter(values))
        if count<=0 or count!=int(count):continue
        counts.append(dict(period=period.isoformat(),value=count))
        caps={number(getattr(r,'total_mv',None)) for r in basics if r.trade_date==period}
        if len(caps)==1 and None not in caps:
            cap=next(iter(caps))
            if cap>0:amounts.append(dict(period=period.isoformat(),value=cap*10000/count))
    return [series('股东户数','户',counts),series('户均持股金额','元',amounts,'#f59e0b')]


def valuation_percentiles(rows,as_of,*,expected_days=(),calendar_complete=False):
    start=as_of.replace(year=as_of.year-5,day=min(as_of.day,28)) if as_of.month==2 else as_of.replace(year=as_of.year-5)
    available={r.trade_date:r for r in rows if start<=r.trade_date<=as_of}
    expected=set(expected_days)
    complete=calendar_complete and bool(expected) and max(expected)==as_of and expected==set(available)
    result={}
    for key in ('pe','pb'):
        points=[number(getattr(r,key,None)) for r in available.values()]
        current=number(getattr(available.get(as_of),key,None))
        valid=[p for p in points if p is not None and p>0]
        result[key+'Percentile']=100*sum(p<=current for p in valid)/len(valid) if complete and all(p is not None for p in points) and current is not None and current>0 and valid else None
    return result


def merge_valuation_rows(history,basics):
    if not history:return tuple(basics)
    # 已验证的原件窗为一个完整来源版本；Core只延伸其真实截止日之后。
    # 不用旧Core填原件冲突/空值，也不因Core空值抹掉有效原件。
    cutoff=max(r.sourceAsOf for r in history)
    merged={r.trade_date:r for r in history}
    merged.update({r.trade_date:r for r in basics if r.trade_date>cutoff})
    return tuple(merged[day] for day in sorted(merged))


def repurchase_evidence(rows,as_of):
    visible=[r for r in rows if r.ann_date and r.ann_date<=as_of]
    return dict(complete=False,annual=[],observedRecords=len(visible),reason='现有记录缺少可核准的方案标识/完成截止日；不把多次进度公告相加为年度回购金额')


def annual_returns(events,profits,as_of,*,price=None,history_complete=False,fiscal_audits=None):
    grouped=defaultdict(list)
    for event in events:
        year=str(event.get('fiscalYear') or str(event.get('periodEnd',''))[:4])
        if len(year)==4 and year.isdigit() and int(year)<=as_of.year:grouped[year].append(event)
    records=[]
    for year in sorted(set(grouped) | set(fiscal_audits or {})):
        items=grouped[year]
        dps=[number(r.get('cashPerShare')) for r in items]
        cash=[number(r.get('cashAmount')) for r in items]
        same_basis=len(items)==1 or (all(number(r.get('stockRatio'))==0 and number(r.get('baseShares')) is not None for r in items) and len({str(r.get('baseShares')) for r in items})==1)
        observed=sum(dps) if items and same_basis and all(v is not None and v>=0 for v in dps) else None
        total=sum(cash) if items and all(v is not None and v>=0 for v in cash) else None
        total=number(total);observed=number(observed)
        profit=number(profits.get(year));complete=history_complete and total is not None and int(year)<as_of.year
        audit=(fiscal_audits or {}).get(year,{})
        ratio=100*total/profit if total is not None and profit is not None and profit>0 else None
        records.append(dict(year=year,observedDps=observed,cashAmount=total,parentProfit=profit,payoutRatio=number(ratio) if complete else None,complete=complete,sourceIds=sorted({s for r in items for s in r.get('sourceIds',[])}),
            **audit,observedPayoutRatio=number(ratio) if audit.get('disclosedQualified') else None))
    by_year={r['year']:r for r in records}
    latest=by_year.get(str(as_of.year-1))
    values=dict(dividendYield=None,payoutRatio=None,expectedDividendYield=None,expectedDividendYieldNTM=None)
    if latest and latest['complete'] and latest['observedDps'] is not None and price and price>0:values['dividendYield']=100*latest['observedDps']/price
    selected=[by_year.get(str(y)) for y in range(as_of.year-3,as_of.year)]
    if all(r is not None and r['complete'] and r['payoutRatio'] is not None for r in selected):values['payoutRatio']=number(sum(r['payoutRatio']/3 for r in selected))
    status='partial' if records else 'missing'
    return dict(annualReturns=records,researchValues=values,
        dividendSeries=[series('已观察每股税前分红小计','元/股',[dict(period=r['year'],value=r['observedDps']) for r in records if r['observedDps'] is not None])],
        returnSeries=[series('已观察分红金额小计','元',[dict(period=r['year'],value=r['cashAmount']) for r in records if r['cashAmount'] is not None])],
        availability={'dividendSeries':dict(status=status,reason='已观察事件小计；财年完整覆盖/资本连续性尚未证明，不作为完整FY或三年支付率'),
            'returnSeries':dict(status=status,reason='分红为已知小计；回购方案身份不完整，未相加')})


def annual_profits(points,as_of):
    groups=defaultdict(list)
    for point in points:
        if point.source_table=='core.income_statement' and point.report_type=='1' and point.end_date<=as_of and point.end_date.month==12 and point.end_date.day==31 and point.visible_at is not None and point.visible_at<=as_of:
            groups[str(point.end_date.year)].append(point)
    result={}
    for year,items in groups.items():
        latest=max(p.visible_at for p in items);items=[p for p in items if p.visible_at==latest]
        flag=max(str(p.values.get('update_flag') or '') for p in items)
        values={number(p.values.get('n_income_attr_p')) for p in items if str(p.values.get('update_flag') or '')==flag}
        result[year]=next(iter(values)) if len(values)==1 else None
    return result


def paid_annual_returns(distributions, as_of, *, price=None):
    """页面与研究值采用同一按截止日累计的财年结果。"""
    from app.stocks.derived.metric_dividends import paid_cash_year_available
    records = []
    for row in distributions:
        # annualDistributions 的 covered 已独立校验查询覆盖，保留金额依赖缺项。
        complete = paid_cash_year_available(row, as_of)
        records.append(row | dict(complete=complete,
            observedPayoutRatio=row.get('payoutRatio') if complete else None))
    by_year = {row['year']: row for row in records}
    latest = by_year.get(str(as_of.year - 1), {})
    dps = number(latest.get('dps'))
    values = dict(dividendYield=100*dps/price if latest.get('complete') and dps is not None and price and price > 0 else None,
                  payoutRatio=None, expectedDividendYield=None, expectedDividendYieldNTM=None)
    selected = [by_year.get(str(y), {}) for y in range(as_of.year - 3, as_of.year)]
    if all(row.get('complete') and row.get('payoutRatio') is not None for row in selected):
        values['payoutRatio'] = number(sum(row['payoutRatio'] / 3 for row in selected))
    status = 'complete' if records and all(row['complete'] for row in records) else 'partial' if records else 'missing'
    reason = f'截至 {as_of.isoformat()} 按实际派息记录累计；缺项按财年及金额依赖列示'
    return dict(annualReturns=records, researchValues=values,
        dividendSeries=[series('已派每股税前现金合计','元/股',[
            dict(period=r['year'],value=number(r['dps'])) for r in records if r.get('dps') is not None])],
        returnSeries=[series('已派现金合计','元',[
            dict(period=r['year'],value=number(r['cashAmount'])) for r in records if r.get('cashAmount') is not None])],
        availability={name: dict(status=status, reason=reason) for name in ('dividendSeries','returnSeries')})


def load_company_projections(factory,symbols,as_of,*,basics=None,financial=None):
    if factory is None:return {}
    from app.stocks.valuation_supply import load_valuation_rows
    history=load_valuation_rows(factory,symbols,as_of)
    basics=basics or {}
    financial=financial or {}
    with factory() as s:
        from app.stocks.shareholder_returns import load_shareholder_returns
        shared_returns = load_shareholder_returns(s, symbols, as_of)
        counts=s.scalars(select(ShareholderCount).where(ShareholderCount.ts_code.in_(symbols))).all()
        repurchases=s.scalars(select(Repurchase).where(Repurchase.ts_code.in_(symbols))).all()
        start=as_of.replace(year=as_of.year-5,day=min(as_of.day,28)) if as_of.month==2 else as_of.replace(year=as_of.year-5)
        calendar=s.scalars(select(TradeCalendar).where(TradeCalendar.exchange=='SSE',TradeCalendar.cal_date>=start,TradeCalendar.cal_date<=as_of)).all()
        calendar_by_day=defaultdict(set)
        for row in calendar:calendar_by_day[row.cal_date].add(row.is_open)
        complete=len(calendar_by_day)==(as_of-start).days+1 and all(len(v)==1 for v in calendar_by_day.values())
        expected=[day for day,status in calendar_by_day.items() if status=={True}]
        if not complete:
            from app.stocks.valuation_calendar import load_calendar
            verified=load_calendar(factory,as_of)
            if verified is not None:expected=verified;complete=True
        result={}
        for code in symbols:
            price_rows = sorted(basics.get(code, ()), key=lambda row: row.trade_date)
            price = number(getattr(price_rows[-1], 'close', None)) if price_rows else None
            annual=paid_annual_returns(shared_returns[code]['annualDistributions'], as_of, price=price)
            annual['concentrationSeries']=concentration([r for r in counts if r.ts_code==code],basics.get(code,()),as_of)
            annual['repurchaseEvidence']=repurchase_evidence([r for r in repurchases if r.ts_code==code],as_of)
            annual['availability'].update(concentrationSeries=dict(status='partial' if annual['concentrationSeries'][0]['points'] else 'missing',reason='户均金额仅在报告期同日总市值齐备时计算；不外推非交易日'),northboundSeries=dict(status='missing',reason='未建立互联互通持仓独立覆盖合同'),executiveTrades=dict(status='missing',reason='尚无已核准高管增减持批量输入'))
            merged=merge_valuation_rows(history.get(code,()),basics.get(code,()))
            annual['researchValues'].update(valuation_percentiles(merged,as_of,expected_days=expected,calendar_complete=complete))
            result[code]=annual
        return result
