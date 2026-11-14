"""可见财报的年度/单季研究投影；不请求供应商，不以其他科目补缺。"""
from math import isfinite
from datetime import date
from app.stocks.derived.inputs import FinancialPoint, visible_financial_rows

FIELDS={
    'revenue':('revenue','total_revenue'),'profit':('n_income_attr_p',),
    'cashflow':('n_cashflow_act',),'capex':('c_pay_acq_const_fiolta',),
    'sales':('c_fr_sale_sg',),'purchases':('c_paid_goods_s',),'cost':('oper_cost',),
    'liabilities':('total_liab',),
    'contractLiability':('contract_liab',), 'receivable':('accounts_receiv',),
    'inventory':('inventories',), 'payable':('acct_payable',),
    'investmentCash':('n_cashflow_inv_act',), 'financingCash':('n_cash_flows_fnc_act',),
    'netIncome':('n_income',),
}
BALANCES = {'liabilities','contractLiability','receivable','inventory','payable'}
CASH_FIELDS = {'cashflow','capex','sales','purchases','investmentCash','financingCash'}
TREND_FIELDS = ('period','revenue','profit','cashflow','investmentCash','financingCash',
                'contractLiability','receivable','inventory','payable','grossMargin','netMargin','revenueBasis')

def _number(value):
    if isinstance(value,bool) or value is None:return None
    try:
        n=float(value)
        return n if isfinite(n) else None
    except (TypeError,ValueError,OverflowError):return None

def _research_reports(points, tables, as_of):
    reports = {}
    def rank(point):
        # 同类型、同可见日比较修订标记和原公告日。完全同版时采用已核验整期原件。
        return (point.report_type != '5', point.visible_at or date.min, point.report_type == '4',
                str(point.values.get('update_flag') or ''), point.announcement_date or date.min)

    def retain(point):
        key = (point.end_date, point.source_table)
        if key not in reports or rank(point) >= rank(reports[key]):
            reports[key] = point

    for source in ('core.balance_sheet','core.income_statement','core.cashflow_statement'):
        candidates = [p for p in points if p.source_table == source and p.report_type in ('1', '4', '5')]
        if as_of:
            candidates = visible_financial_rows([p for p in candidates if p.end_date <= as_of], as_of)
        for point in sorted(candidates, key=lambda p: p.source_record_key or ''):
            retain(point)
    sources = {'income':'core.income_statement','balance':'core.balance_sheet','cashflow':'core.cashflow_statement'}
    for table in tables:
        source = sources.get(table.get('kind'))
        if not source or table.get('amountUnit') != 'yuan':
            continue
        metadata = {m['period']:m for m in table.get('reportMetadata', [])}
        for index, period in enumerate(table['periods']):
            meta = metadata.get(period, {})
            announced = meta.get('announcementDate') or ''
            if meta.get('reportType') != '1' or not period <= announced <= as_of.isoformat():
                continue
            end = date.fromisoformat(period)
            visible = date.fromisoformat(announced)
            # 已核验整期记录优先，冲突或空字段不拼接其他版本的数值。
            values = {r['label']:r['values'][index] for r in table['rows'] if index < len(r['values'])}
            values.update(update_flag=meta.get('updateFlag'), ann_date=meta.get('originalAnnouncementDate'))
            record = FinancialPoint(end,visible,'1',values,source_table=source)
            if record.disclosed_by(as_of):
                retain(record)
    return reports.values()

def financial_research(points, *, tables=(), as_of=None):
    raw={}
    for point in _research_reports(tuple(points),tables,as_of):
        values=raw.setdefault(point.end_date.isoformat(),{})
        for key,names in FIELDS.items():
            source='balance' if key in BALANCES else 'cashflow' if key in CASH_FIELDS else 'income'
            table = 'core.balance_sheet' if source == 'balance' else f'core.{source}_statement'
            if point.source_table != table:continue
            for name in names:
                v=_number(point.values.get(name))
                if v is not None:
                    values[key]=v/1e8
                    break
    rows=[{'period':period,**{key:values.get(key) for key in FIELDS}} for period,values in sorted(raw.items())]
    annual=[r for r in rows if r['period'].endswith('-12-31')]
    quarter=[]
    for r in rows:
        month=int(r['period'][5:7]); previous_month=month-3
        previous=next((p for p in rows if p['period'][:4]==r['period'][:4] and int(p['period'][5:7])==previous_month),None)
        q={'period':r['period']}
        for key in FIELDS:
            v=r[key];prior=previous.get(key) if previous else None
            q[key]=v if month==3 or key in BALANCES else v-prior if v is not None and prior is not None else None
        quarter.append(q)
    quarterly_revenue = {int(r['period'][:4])*4+int(r['period'][5:7])//3:r['revenue'] for r in quarter}
    for series, single_quarter in ((annual, False), (quarter, True)):
        for r in series:
            revenue = r['revenue']
            r['grossMargin'] = 100*(revenue-r['cost'])/revenue if revenue and r['cost'] is not None else None
            r['netMargin'] = 100*r['netIncome']/revenue if revenue and r['netIncome'] is not None else None
            r['revenueBasis'] = revenue
            if single_quarter:
                index = int(r['period'][:4])*4+int(r['period'][5:7])//3
                trailing = [quarterly_revenue.get(index-i) for i in range(4)]
                r['revenueBasis'] = sum(trailing) if None not in trailing else None
    # 无独立FY归属/三年平均或预测声明时，不拿TTM/单期代替。
    values={'dividendYield':None,'payoutRatio':None,'capitalReturnRatio':None,'expectedDividendYield':None,'expectedDividendYieldNTM':None}
    definitions={'realMoneyIndex':('cashflow','profit'),'ocfToRevenue':('cashflow','revenue'),'ocfToTotalLiab':('cashflow','liabilities'),
        'capexCoverageRatio':('cashflow','capex'),'cashCollectionRatio':('sales','revenue'),'cashPaymentRatio':('purchases','cost')}
    missing = {}
    labels = {'cashflow':'经营现金流','profit':'归母净利润','revenue':'营业收入','liabilities':'负债总额',
              'capex':'资本开支','sales':'销售收现','purchases':'购货付现','cost':'营业成本'}
    for years in (1,3,5,10):
        selected=annual[-years:]
        valid=len(selected)==years and all(int(b['period'][:4])==int(a['period'][:4])+1 for a,b in zip(selected,selected[1:]))
        suffix='' if years==1 else f'Cum{years}y'
        for name,(n,d) in definitions.items():
            complete=valid and all(r[n] is not None and r[d] is not None for r in selected)
            numerator=sum(r[n] for r in selected) if complete else None
            denominator=sum(r[d] for r in selected) if complete else None
            values[name+suffix]=numerator/denominator if complete and denominator!=0 and isfinite(numerator/denominator) else None
            if values[name+suffix] is None:
                if not valid:
                    missing[name+suffix] = f'缺连续 {years} 个完整财年的年报'
                elif denominator == 0:
                    missing[name+suffix] = f'{selected[0]["period"]} 至 {selected[-1]["period"]} 的{labels[d]}分母合计为零'
                else:
                    missing[name+suffix] = '；'.join(
                        f'{r["period"]} 缺{labels[k]}' for r in selected for k in (n,d) if r[k] is None)
        complete=valid and all(r['cashflow'] is not None and r['capex'] is not None and r['capex']>=0 for r in selected)
        fcf=sum(r['cashflow']-r['capex'] for r in selected) if complete else None
        revenue=sum(r['revenue'] for r in selected) if complete and all(r['revenue'] is not None for r in selected) else None
        fcf=fcf if fcf is not None and isfinite(fcf) else None
        values['freeCashFlowFy'+suffix]=fcf
        ratio=fcf/revenue if fcf is not None and revenue not in (None,0) else None
        values['freeCashFlowMarginFy'+suffix]=ratio if ratio is not None and isfinite(ratio) else None
        for key in ('freeCashFlowFy','freeCashFlowMarginFy'):
            if values[key+suffix] is not None:
                continue
            if not valid:
                missing[key+suffix] = f'缺连续 {years} 个完整财年的年报'
            elif key == 'freeCashFlowMarginFy' and revenue == 0:
                missing[key+suffix] = '同期营业收入分母合计为零'
            else:
                fields = ['cashflow','capex'] + (['revenue'] if key == 'freeCashFlowMarginFy' else [])
                missing[key+suffix] = '；'.join(f'{r["period"]} 缺有效{labels[k]}'
                    for r in selected for k in fields if r[k] is None or (k == 'capex' and r[k] < 0))
    for key,field in [('revenueGrowingFy','revenue'),('ocfGrowingFy','cashflow')]:
        pair=annual[-2:]
        valid=len(pair)==2 and int(pair[1]['period'][:4])==int(pair[0]['period'][:4])+1 and all(r[field] is not None for r in pair) and pair[0][field]!=0
        values[key]=(1 if (pair[1][field]-pair[0][field])/abs(pair[0][field])>0 else 0) if valid else None
    return {'researchValues':values,'listMissingReasons':{k:v for k,v in missing.items() if v},'profitTrend':[r['profit'] for r in annual[-3:]],
        'financialTrends':{name:[{k:r[k] for k in TREND_FIELDS} for r in sequence[-8:]]
                           for name,sequence in [('annual',annual),('quarterly',quarter)]}}
