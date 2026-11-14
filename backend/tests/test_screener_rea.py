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
