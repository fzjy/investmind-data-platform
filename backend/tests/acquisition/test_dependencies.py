from app.acquisition.availability.published import _item
from app.acquisition.contracts import StateKey


def test_different_dividend_cards_are_independent():
    payload = {'symbol':'600519.SH','metrics':{'cards':[{'id':'workbenchFiscalDividendYield','value':'0.00%'}]}}
    def item(name):
        return _item(StateKey('stock-dividend-yield',name,'security','600519.SH'),'stock-workbench:600519.SH',payload,None)
    assert item('dividendYield').kind == 'available'
    assert item('expectedDividendYieldNTM').kind == 'missing'


def test_macro_published_value_requires_matching_metadata_period():
    key = StateKey('cn.cpi.yoy','','series','cn.cpi.yoy')
    payload = {'sandboxIndicators':[{'id':key.entry_id,'values':[{'month':'2020-01-01','value':0}]}],
               'seriesStatus':[{'id':key.entry_id,'observedAt':'2022-03-01','actualSource':'nbs'}]}
    assert _item(key,'macro-insights',payload,None).kind == 'unknown'
    payload['seriesStatus'][0]['observedAt'] = '2020-01-01'
    assert _item(key,'macro-insights',payload,None).kind == 'available'
