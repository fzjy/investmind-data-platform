from copy import deepcopy

from app.shared.contracts import DataMeta, ProviderSnapshot
from app.stocks.models import ScreenerRequest
from app.stocks.service import ScreenerService
from tests.test_screener import default_request, stock, StaticStocksProvider

def query(rows, mutate=lambda r: None):
    request=default_request()
    request['filters']['boards']=['sh-main','sz-main','star','chinext','bse']
    request['filters']['enabledThresholds']=[]
    mutate(request)
    provider=StaticStocksProvider(ProviderSnapshot(raw={'bootstrap':{'totalCount':len(rows)},'stocks':deepcopy(rows)},meta=DataMeta(source='fixture',asOf='2026-10-28',version='rea-test',stale=False)))
    return ScreenerService(provider).query(ScreenerRequest.model_validate(request)).data

def row(symbol='600001.SH', pe=8):
    return stock(symbol,dividend_yield=4,pe=pe,industry='测试')

def test_explicit_enabled_at_default_and_empty_board():
    assert query([row()],lambda r:r['filters'].update(boards=[]))['total']==0
    assert query([row(pe=None)],lambda r:r['filters'].update(enabledThresholds=['pe']))['total']==0
    assert query([row(pe=None)])['total']==1

def test_pe_strict_positive_and_unknown_preserved():
    def enable(r):
        r['filters']['enabledThresholds']=['pe'];r['filters']['thresholds']['pe']=10
    assert query([row(pe=p) for p in [-1,0,9,10,None]],enable)['total']==1
    a=row();a.update(inventoryRisk=None,netDebt=None,realMoneyIndex=None)
    assert query([a],lambda r:r['filters'].update(exclusions=['inventoryRisk','netDebt','lowRealMoney']))['total']==1

def test_weighted_sort_uses_full_filtered_population_before_pagination():
    rows=[]
    for i,(dy,roe) in enumerate([(0,100),(40,60),(100,0)]):
        a=row(f'{i:06d}.SH');a.update(dividendYield=dy,roe=roe);rows.append(a)
    def sort(r):
        r['sortRules']=[dict(field='dividendYield',direction='desc',enabled=True,weight=20),dict(field='roe',direction='desc',enabled=True,weight=80)]
        r['pageSize']=1
    result=query(rows,sort)
    assert result['total']==3 and result['items'][0]['symbol']=='000000.SH'

def test_pagination_can_reach_201st_without_changing_universe():
    rows=[row(f'{i:06d}.SH') for i in range(261)]
    result=query(rows,lambda r:r.update(page=2,pageSize=200))
    assert result['total']==261 and len(result['items'])==61

def test_known_legacy_three_year_percentile_cannot_pass_five_year_condition():
    a=row();a.update(pePercentile=20,metricBasis={'realMoneyIndex':'annual-OCF/parent-profit'},researchValues={})
    def enable(r):
        r['filters']['enabledThresholds']=['pePercentile'];r['filters']['thresholds']['pePercentile']=30
    assert query([a],enable)['total']==0

def test_roic_uses_disclosed_annual_percent_within_display_window_and_keeps_raw_input():
    records = []
    for i, reports in enumerate([
        [{'period':'2024-12-31','frequency':'FY','roic':12.5}, {'period':'2025-06-30','frequency':'YTD','roic':4}],
        [{'period':'2024-12-31','frequency':'FY','roic':0}],
        [],
        [{'period':f'{y}-12-31','frequency':'FY','roic':20 if y == 2010 else None} for y in range(2010,2019)],
    ]):
        a = row(f'{i:06d}.SH')
        a.update(roic=0.4, researchValues={'roic':0.4}, listHistory={'returns':reports})
        records.append(a)
    data = query(records, lambda r:r.update(sortMode='quick', sortRules=[dict(field='roic',direction='desc',enabled=True)]))
    assert [item['roic'] for item in data['items']] == [12.5, 0, None, None]
    assert [item['researchValues']['roic'] for item in data['items']] == [12.5, 0, None, None]
    assert data['bootstrap']['availableCounts']['roic'] == 2
    def enable(r):
        r['filters']['thresholds']['roic'] = 10
        r['filters']['enabledThresholds'] = ['roic']
    assert [item['symbol'] for item in query(records, enable)['items']] == ['000000.SH']
    assert records[0]['roic'] == 0.4

def test_legacy_missing_or_malformed_projection_keeps_other_fields_and_zero():
    valid = {'frequency':'FY','period':'2024-12-31','roic':0}
    invalid = [None, 'FY', {}, {'frequency':'FY', 'period':None, 'roic':80},
               {'frequency':'FY', 'period':'2025-06-30', 'roic':80}]
    for history in (None, 'old', {}, {'returns':None}, {'returns':{}}, {'returns':invalid}):
        record = row()
        record.update(listHistory=history, metricBasis=None, researchValues=None, listMissingReasons=[])
        actual = query([record])['items'][0]
        assert actual['roic'] is actual['researchValues']['roic'] is None
        assert actual['listMissingReasons']['roic']
    record = row()
    record.update(listHistory={'returns':[valid, *invalid]}, researchValues={'other':4},
                  metricBasis={'other':'ok'}, listMissingReasons=None)
    actual = query([record])['items'][0]
    assert actual['roic'] == 0 and actual['researchValues'] == {'other':4, 'roic':0}
    assert actual['metricBasis']['other'] == 'ok'
    assert 'roic' not in actual['listMissingReasons']
