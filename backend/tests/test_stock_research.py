from datetime import date
from app.stocks.derived.inputs import FinancialPoint
from app.stocks.research import financial_research

def point(period, table, **values):
    return FinancialPoint(date.fromisoformat(period),date(2026,1,1),'1',values,'600001.SH',f'core.{table}_statement')

def test_annual_ratios_use_real_receipts_payments_and_capex_and_sum_legs():
    rows=[]
    for year,ocf,capex in [(2022,10,5),(2023,30,5),(2024,50,10)]:
        rows.extend([point(f'{year}-12-31','income',revenue=100e8,oper_cost=50e8,n_income_attr_p=20e8),point(f'{year}-12-31','cashflow',n_cashflow_act=ocf*1e8,c_pay_acq_const_fiolta=capex*1e8,c_fr_sale_sg=110e8,c_paid_goods_s=40e8)])
    r=financial_research(rows)
    assert r['researchValues']['cashCollectionRatio']==1.1
    assert r['researchValues']['cashPaymentRatio']==.8
    assert r['researchValues']['capexCoverageRatioCum3y']==4.5
    assert r['researchValues']['freeCashFlowFy']==40
    assert r['researchValues']['realMoneyIndexCum5y'] is None

def test_quarterly_subtracts_cumulative_same_year_and_missing_previous_is_not_zero():
    r=financial_research([point('2025-03-31','income',revenue=10e8),point('2025-06-30','income',revenue=25e8),point('2025-12-31','income',revenue=70e8)])
    assert [p['revenue'] for p in r['financialTrends']['quarterly']]==[10,15,None]

def test_missing_leg_and_gap_year_do_not_create_aggregate_or_fcf():
    rows=[point('2022-12-31','cashflow',n_cashflow_act=10e8),point('2024-12-31','cashflow',n_cashflow_act=20e8,c_pay_acq_const_fiolta=5e8)]
    r=financial_research(rows)
    assert r['researchValues']['capexCoverageRatioCum3y'] is None
    assert r['researchValues']['cashCollectionRatio'] is None

def test_screener_balance_is_a_stock_and_quarterly_sales_basis_requires_four_quarters():
    from dataclasses import replace
    inputs = []
    for i, period in enumerate(['2024-03-31','2024-06-30','2024-09-30','2024-12-31'], start=1):
        inputs.append(point(period, 'income', revenue=i*10e8, oper_cost=i*8e8))
        inputs.append(replace(point(period,'balance',accounts_receiv=i*2e8),source_table='core.balance_sheet'))
    trends = financial_research(inputs)['financialTrends']
    assert [p['receivable'] for p in trends['quarterly']] == [2,4,6,8]
    assert [p['revenueBasis'] for p in trends['quarterly']] == [None,None,None,40]
    assert trends['annual'][0]['grossMargin'] == 20

def test_workbench_metric_source_does_not_replace_parent_profit_with_total_profit():
    from app.stocks.marts.stocks import _metric_overview
    from backend.tests.marts.test_screener_builder import _core_snapshot
    result=_metric_overview(_core_snapshot(),'600001.SH',(),[point('2024-12-31','income',revenue=100e8,total_revenue=110e8,n_income=20e8)])
    fields={f['id']:f for f in result['fields']}
    # 缺值保留其年度位置，不能用合并净利润填入归母利润。
    assert fields['parentNetProfit']['values']==[None]
    assert fields['revenue']['values']==[100]

def test_screener_uses_visible_revision_only_and_keeps_balance_without_ttm():
    from dataclasses import replace
    period = '2025-12-31'
    early = replace(point(period, 'income', revenue=10e8), visible_at=date(2026, 3, 1))
    late = replace(early, visible_at=date(2026, 6, 1), values={'revenue':20e8})
    balance = replace(early, source_table='core.balance_sheet', values={'accounts_receiv':2e8})
    result = financial_research([late, early, balance], as_of=date(2026, 4, 1))
    q = result['financialTrends']['quarterly'][0]
    assert q['receivable'] == 2 and q['revenueBasis'] is None
    assert result['financialTrends']['annual'][0]['revenue'] == 10
    assert financial_research([early, late], as_of=date(2026, 4, 1)) == financial_research([early], as_of=date(2026, 4, 1))

def test_verified_statement_and_core_use_same_consolidated_revision_order():
    from dataclasses import replace
    period = '2025-12-31'
    table = {'kind':'income', 'amountUnit':'yuan','periods':[period],
             'reportMetadata':[{'period':period,'reportType':'1','announcementDate':'2026-03-02'}],
             'rows':[{'label':'revenue','values':[12e8]}]}
    adjusted = replace(point(period,'income',revenue=20e8), visible_at=date(2026,3,2), report_type='4')
    backup = replace(adjusted, visible_at=date(2026,4,3), report_type='5', values={'revenue':1e8})
    def revenue(points):
        result = financial_research(points,tables=[table],as_of=date(2026,5,1))
        return result['financialTrends']['annual'][0]['revenue']
    assert revenue([adjusted, backup]) == 20
    assert revenue([backup]) == 12

def test_source_selection_preserves_core_revision_flag_and_original_announcement():
    from dataclasses import replace
    from app.stocks.financial_supply import statement_tables
    def report(value, flag, ann):
        return replace(point('2025-12-31','income', revenue=value*1e8, update_flag=flag, ann_date=ann),
                       visible_at=date(2026,3,5))
    cutoff = date(2026,5,1)
    for original,core in ((report(11,'0','20260304'), report(13,'1','20260301')),
                         (report(12,'1','20260301'), report(13,'1','20260303'))):
        tables = statement_tables([original], cutoff)
        metadata = next(t for t in tables if t['kind']=='income')['reportMetadata'][0]
        assert metadata['updateFlag'] == original.values['update_flag']
        assert metadata['originalAnnouncementDate'] == original.announcement_date.isoformat()
        results = financial_research([core], tables=tables, as_of=cutoff)
        assert results['financialTrends']['annual'][0]['revenue'] == 13
        # 较新的原件同样应能替换较旧的 Core。
        results = financial_research([original], tables=statement_tables([core], cutoff), as_of=cutoff)
        assert results['financialTrends']['annual'][0]['revenue'] == 13
