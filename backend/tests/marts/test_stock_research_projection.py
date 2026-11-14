from copy import deepcopy
from datetime import datetime,timezone
from app.marts.contracts import MartCandidate
from app.stocks.marts.stock_research_runtime import project_research_snapshots


def test_net_cash_requires_all_six_debt_fields_from_one_consolidated_report():
    from types import SimpleNamespace
    from datetime import date
    from app.stocks.marts.stocks import _metric_overview
    fields = dict(money_cap=4e8, st_borr=0, lt_borr=1e8, st_bonds_payable=0,
                  non_cur_liab_due_1y=0, bond_payable=0, lease_liab=0)
    row = SimpleNamespace(end_date=date(2026, 6, 30), source_table="core.balance_sheet", report_type="1", values=fields)
    def cards():
        return _metric_overview(None, "600001.SH", (), [row])["cards"]
    assert cards()[0]["value"] == "3.00 亿元"
    assert "2026-06-30" in cards()[0]["detail"]
    del fields["lease_liab"]
    assert not cards()

def test_projection_preserves_quotes_charts_risks_shareholders_and_non_target_products():
    when=datetime(2026,10,28,tzinfo=timezone.utc)
    workbench={'quote':{'price':10},'dailyChart':{'candles':[1]},'risks':{'score':None},'metrics':{'cards':[{'label':'original'}]},'financial':{'dashboard':{'products':[1],'bands':[2]},'shareholders':{'holders':[3]},'statements':{'diagnosis':[4],'tables':[]}}}
    current={'screener':{'stocks':[{'symbol':'600001.SH','dividendYield':5}],'bootstrap':{}},'stock-workbench:600001.SH':workbench}
    old=deepcopy(current)
    fresh=MartCandidate('stock-workbench:600001.SH',when,{'quote':{'marketCap':10},'metrics':{'fields':[1],'years':['2025'],'presets':[]},'financial':{'years':['2025'],'reportTypes':['FY'],'announcementDate':'2026-04-01','statements':{'tables':[1]},'dashboard':{'trends':[2]}}})
    screen=MartCandidate('screener',when,{'stocks':[{'symbol':'600001.SH','realMoneyIndex':1.1,'researchValues':{'x':1},'financialTrends':{},'listRisks':[{'key':'netCash'}],'listMissingReasons':{'x':'缺输入'}}],'bootstrap':{}})
    projected=project_research_snapshots(current,screen,[fresh])
    p=projected[1].payload
    assert current==old and p['dailyChart']==workbench['dailyChart'] and p['risks']==workbench['risks']
    assert p['quote']=={'price':10,'marketCap':10}
    assert p['financial']['shareholders']==workbench['financial']['shareholders']
    assert p['financial']['dashboard']['products']==[1] and p['financial']['statements']['diagnosis']==[4]
    assert projected[0].payload['stocks'][0]['dividendYield']==5
    for key in ('listRisks','listMissingReasons'):
        assert projected[0].payload['stocks'][0][key] == screen.payload['stocks'][0][key]

def test_projection_rejects_scope_changes_or_missing_workbench():
    import pytest
    when=datetime(2026,10,28,tzinfo=timezone.utc)
    current={'screener':{'stocks':[{'symbol':'600001.SH'}]}}
    with pytest.raises(ValueError):project_research_snapshots(current,MartCandidate('screener',when,{'stocks':[{'symbol':'600002.SH'}]}),[])
