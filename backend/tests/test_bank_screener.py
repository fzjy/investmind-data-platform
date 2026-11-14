from copy import deepcopy
import pytest
from pydantic import ValidationError
from app.shared.contracts import DataMeta,ProviderSnapshot
from app.stocks.models import ScreenerRequest
from app.stocks.service import ScreenerService
from tests.test_screener import default_request,stock,StaticStocksProvider

def settings(**kw):
    return dict(divisor=5,weights=dict(yield_=100,pb=100,large=100,nim=100,provision=100),conditions=[],sort='code') | kw

def request(**kw):
    r=settings(**kw);r['weights']={('yield' if k=='yield_' else k):v for k,v in r['weights'].items()}
    return r

def inputs(**kw):
    return dict(revisedYield=6,revisedGrowth=10,pb=0.5,offensivePbScore=None,largeBankScore=0.5,nim=1.8,yearEndNim=1.7,provision=280,yearEndProvision=250) | kw

def test_scoring_five_raw_components_presets_and_weights_are_recovered_rules():
    from app.stocks.bank_scoring import score_bank,rules
    r=score_bank(inputs(),request())
    assert r['scores']=={'yield':8,'pb':2,'large':0.5,'nim':1,'provision':1}
    assert r['total']==12.5 and r['enabledCount']==5
    for preset in rules()['presets'].values():
        result=score_bank(inputs(),request(divisor=preset['divisor'],weights=preset['weights']))
        assert result['total']==pytest.approx(sum(result['scores'][k]*v/100 for k,v in preset['weights'].items()))

def test_n3_prefers_zero_offensive_pb_and_provision_negative_is_not_capped():
    from app.stocks.bank_scoring import score_bank
    r=score_bank(inputs(offensivePbScore=0,provision=200),request(divisor=3))
    assert r['scores']['pb']==0 and r['scores']['provision']==-5
    assert score_bank(inputs(nim=1.7),request())['scores']['nim']==0
    assert score_bank(inputs(nim=1.6),request())['scores']['nim']==-1

def test_missing_enabled_inputs_make_total_unknown_but_zero_weight_does_not():
    from app.stocks.bank_scoring import score_bank
    r=score_bank(inputs(revisedGrowth=None,largeBankScore=None),request())
    assert r['total'] is None and r['scores']['yield'] is None and r['scores']['large'] is None
    r=score_bank(inputs(revisedGrowth=None,largeBankScore=None),request(weights={'yield':0,'pb':100,'large':0,'nim':0,'provision':0}))
    assert r['total']==2 and r['enabledCount']==1
    assert score_bank({},request(weights={k:0 for k in ('yield','pb','large','nim','provision')}))['total']==0

@pytest.mark.parametrize('value',[None,False,0,-1,float('nan'),float('inf')])
def test_bad_pb_never_becomes_valid_score(value):
    from app.stocks.bank_scoring import score_bank
    r=score_bank(inputs(pb=value),request())
    assert r['scores']['pb'] is None and r['total'] is None

def test_unknown_large_bank_and_overflow_do_not_get_zero_or_infinite_total():
    from app.stocks.bank_scoring import score_bank
    assert score_bank(inputs(largeBankScore=2),request())['scores']['large'] is None
    r=score_bank(inputs(revisedYield=1.7e308,revisedGrowth=1.7e308),request(divisor=3))
    assert r['scores']['yield'] is None and r['total'] is None

def test_request_normalizes_weights_by5_without_bankers_rounding_and_halfstep_N():
    from app.stocks.bank import BankRequest
    r=BankRequest.model_validate(request(divisor=3.5,weights={'yield':7.5,'pb':122,'large':0,'nim':100,'provision':100}))
    assert r.model_dump(by_alias=True)['weights']['yield']==10
    assert r.model_dump(by_alias=True)['weights']['pb']==120

@pytest.mark.parametrize('bad',[
    {'divisor':3.2},{'divisor':True},{'divisor':float('nan')},
    {'weights':{'yield':100,'pb':201,'large':0,'nim':0,'provision':0}},
    {'conditions':[{'field':'roe','operator':'gt','value':False}]},
    {'conditions':[{'field':'not-known','operator':'gt','value':1}]},
    {'conditions':[{'field':'roe','operator':'gt','value':1}]*2},
    {'inputs':{'revisedYield':99}},
])
def test_bad_requests_cannot_override_rules_or_inject_values(bad):
    from app.stocks.bank import BankRequest
    with pytest.raises(ValidationError):BankRequest.model_validate(request()|bad)

def query(rows,bank=None,patch=None):
    raw=default_request();raw['filters']['enabledThresholds']=[]
    if bank is not None:raw['bank']=bank
    if patch:patch(raw)
    provider=StaticStocksProvider(ProviderSnapshot(raw={'bootstrap':{'totalCount':len(rows)},'stocks':deepcopy(rows)},meta=DataMeta(source='fixture',asOf='2026-10-21',version='bank-test',stale=False)))
    return ScreenerService(provider).query(ScreenerRequest.model_validate(raw)).data

def banks():
    return [stock('600036.SH',dividend_yield=6,pe=5,industry='银行'),stock('000001.SZ',dividend_yield=5,pe=6,industry='银行'),stock('600519.SH',dividend_yield=3,pe=20,industry='白酒')]

def test_lock_bank_before_pagination_ignore_ordinary_threshold_and_major_industry_only():
    rows=banks()
    def patch(r):
        r['filters']['categories']['majorIndustry']=['白酒']
        r['filters']['thresholds']['pe']=0;r['filters']['enabledThresholds']=['pe']
        r['pageSize']=1;r['page']=2
    r=query(rows,request(),patch)
    assert r['total']==2 and len(r['items'])==1 and r['items'][0]['majorIndustry']=='银行'
    assert r['bank']['universeCount']==2
    assert query(rows)['total']==3 and 'bank' not in query(rows)

def test_other_categories_and_boards_preserved_missing_bank_field_filters_exclude():
    rows=banks()
    assert query(rows,request(),lambda r:r['filters']['boards'].clear())['total']==0
    assert query(rows,request(conditions=[dict(field='netInterestMargin',operator='gt',value=0)]))['total']==0
    assert query(rows,request(),lambda r:r['filters']['categories'].__setitem__('subIndustry',['missing']))['total']==0

def test_bank_response_binds_every_effective_filter_even_empty_results():
    rows=banks()
    def patch(r):
        r['filters']['boards']=[];r['filters']['exclusions']=['st']
        r['filters']['categories']=dict(majorIndustry=['白酒'],subIndustry=['银行'],concept=['价值'],region=['上海'])
    result=query(rows,request(),patch)
    assert result['total']==0
    assert result['bank']['selection']==dict(boards=[],exclusions=['st'],subIndustry=['银行'],concept=['价值'],region=['上海'])

def test_projection_uses_published_pb_not_unverified_manual_bank_fields_or_fy_proxy():
    rows=banks();original=deepcopy(rows)
    for row in rows:
        row['bankEvaluation']={'inputs':inputs(),'rulesVersion':'bank-sheet-v4','sourceSha':'pretend'}
    r=query(rows,request())
    assert r['total']==2
    for row in r['items']:
        e=row['bankEvaluation']
        assert e['inputs']['pb']==2 and e['inputs']['revisedYield'] is None
        assert e['fields']['dividendYield'] is None
        assert e['total'] is None and e['ahDiscount'] is None
        assert e['algorithmVersion']=='rea-bank-score-20261020'
        assert e['reasons']['yield'] and e['sources']['revisedYield']
    assert all('bankEvaluation' not in row for row in original)

def test_positive_pb_conditions_sort_and_unknown_last_work_on_whole_pool():
    rows=banks();rows[0]['pb']=1;rows[1]['pb']=0.5
    onlypb=request(weights={'yield':0,'pb':100,'large':0,'nim':0,'provision':0},sort='score-desc')
    r=query(rows,onlypb)
    assert [s['symbol'] for s in r['items']]==['000001.SZ','600036.SH']
    assert [s['bankEvaluation']['total'] for s in r['items']]==[2,1]
    r=query(rows,onlypb|dict(conditions=[dict(field='pb',operator='lt',value=0.7)]))
    assert r['total']==1 and r['items'][0]['symbol']=='000001.SZ'
    rows[1]['pb']=0
    assert query(rows,onlypb)['items'][-1]['symbol']=='000001.SZ'

def test_ah_formula_sign_and_required_positive_legs_do_not_supply_actual_missing_data():
    from app.stocks.bank_scoring import ah_discount
    assert ah_discount(10,8,1)==-20
    assert ah_discount(10,10,1.2)==20
    for bad in (None,False,0,-1,float('nan')):
        assert ah_discount(bad,8,1) is None
    assert ah_discount(1e-308,1e308,1e308) is None
