from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.shared.contracts import DataMeta, ProviderSnapshot
from app.shared.errors import DataServiceError


class Provider:
    def __init__(self, stocks):
        self.raw = {'bootstrap': {}, 'stocks': stocks}
        self.meta = DataMeta(source='database', asOf='2026-10-21', version='fixed', stale=False)
        self.work = {}

    def read_screener_universe(self):
        return ProviderSnapshot(deepcopy(self.raw), self.meta)

    def read_workbench(self, symbol):
        raw, meta = self.work.get(symbol, (None, self.meta))
        return ProviderSnapshot(raw, meta)


def row(symbol='000001.SZ', **values):
    return {'symbol': symbol, 'name': symbol, 'industry': '银行', 'majorIndustry': '银行',
            'currency': 'CNY', 'pe': 10, 'pb': 1, 'marketCap': 100, **values}


def query(provider, **values):
    from app.stocks.research_tools import ResearchRequest, ResearchToolsService
    return ResearchToolsService(provider).query(ResearchRequest(symbol='000001.SZ', **values)).data


def metric(data, key):
    return next(m for m in data['metrics'] if m['key'] == key)


def work(provider, symbol, closes, **values):
    provider.work[symbol] = ({'symbol': symbol, 'quote': {'symbol': symbol, 'market': 'CN'},
                            'dailyChart': {'candles': [{'date': d, 'close': c} for d, c in closes]},
                            'isFallbackSample': False, **values}, provider.meta)


def test_full_published_universe_used_before_statistics_and_no_mutation():
    provider = Provider([row(f'{i:06d}.SZ', pe=i, marketCap=i) for i in range(1, 302)])
    before = deepcopy(provider.raw)
    data = query(provider)
    assert data['scope']['universeCount'] == 301
    assert len(data['instruments']) == 301
    assert data['peer']['symbol'] == '000301.SZ'
    assert metric(data, 'pe')['sampleCount'] == 301
    assert metric(data, 'pe')['percentile'] == pytest.approx(100 / 301)
    assert provider.raw == before


def test_ties_missing_nonpositive_and_constant_variance_keep_explicit_meaning():
    provider = Provider([row(pe=10), row('000002.SZ', pe=10), row('000003.SZ', pe=-1),
                         row('000004.SZ', pe=None), row('000005.SZ', pe=False)])
    data = query(provider)
    pe = metric(data, 'pe')
    assert pe['percentile'] == 100 and pe['sampleCount'] == 2 and pe['zScore'] is None
    provider.raw['stocks'][0]['pe'] = -1
    assert metric(query(provider), 'pe')['value'] is None


def test_qualified_fy_and_forecasts_do_not_fall_back_to_reference_or_demo_values():
    data = query(Provider([row(dividendYield=12, expectedDividendYield=16, netCash=999)]))
    for key in ('dividendYield', 'expectedDividendYield', 'expectedDividendYieldNTM', 'netCash'):
        assert metric(data, key)['value'] is None
    assert data['peer'] is None and data['pair']['status'] == 'unavailable'
    assert metric(data, 'pe')['percentile'] is None  # 单样本不报100%分位


def test_difference_units_and_sample_standard_deviation_are_exact():
    p = Provider([row(pe=10, researchValues={'dividendYield': 3}),
                  row('000002.SZ', pe=20, researchValues={'dividendYield': 1}),
                  row('000003.SZ', pe=30, industry='煤炭', majorIndustry='煤炭')])
    data = query(p, peerSymbol='000002.SZ')
    assert metric(data, 'pe')['label'] == 'PE（静态）'
    assert metric(data, 'pe')['zScore'] == -1
    assert metric(data, 'pe')['industrySampleCount'] == 2
    assert metric(data, 'dividendYield')['difference'] == 2


@pytest.mark.parametrize('values', [{'symbol': True}, {'symbol': 'X'}, {'symbol': '000001.SZ', 'price': 1},
                                  {'symbol': '000001.SZ', 'peerSymbol': '000001.SZ'}])
def test_strict_request_rejects_injected_data_and_self_pair(values):
    from app.stocks.research_tools import ResearchRequest
    with pytest.raises((ValidationError, DataServiceError)):
        ResearchRequest.model_validate(values)


def test_pool_identity_and_peer_recommendation_are_conservative_and_stable():
    p = Provider([row(), row('000003.SZ'), row('000002.SZ'),
                  row('000004.SZ', majorIndustry='煤炭', marketCap=100000)])
    assert query(p)['peer']['symbol'] == '000002.SZ'
    with pytest.raises(DataServiceError):
        query(p, peerSymbol='999999.SZ')
    p.raw['stocks'].append(row())
    with pytest.raises(DataServiceError):
        query(p)


def test_same_date_published_chart_ratio_does_not_fill_gaps_or_look_ahead():
    p = Provider([row(), row('000002.SZ')])
    work(p, '000001.SZ', [('2026-10-19', 12), ('2026-10-20', 14), ('2026-10-21', 18), ('2026-10-22', 99)])
    work(p, '000002.SZ', [('2026-10-19', 3), ('2026-10-21', 6)])
    pair = query(p)['pair']
    assert pair['status'] == 'available' and pair['ratio'] == 3
    assert pair['points'] == [{'date': '2026-10-19', 'value': 4}, {'date': '2026-10-21', 'value': 3}]
    assert pair['asOf'] == '2026-10-21' and pair['basis'] == 'published-chart-close-ratio'
    assert '复权完整性未核验' in pair['reason']


@pytest.mark.parametrize('kind', ['version', 'date', 'source', 'fallback', 'symbol', 'currency', 'conflict', 'invalid-date', 'bad-value'])
def test_pair_rejects_unverifiable_legs(kind):
    p = Provider([row(), row('000002.SZ')])
    work(p, '000001.SZ', [('2026-10-21', 12)])
    work(p, '000002.SZ', [('2026-10-21', 3)])
    raw, meta = p.work['000002.SZ']
    if kind in ('version', 'date', 'source'):
        meta = meta.model_copy(update={dict(version='version', date='as_of', source='source')[kind]:
                                      dict(version='old', date='2026-10-20', source='fixture')[kind]})
        p.work['000002.SZ'] = (raw, meta)
    elif kind == 'fallback': raw['isFallbackSample'] = True
    elif kind == 'symbol': raw['symbol'] = '999999.SZ'
    elif kind == 'currency': p.raw['stocks'][1]['currency'] = 'HKD'
    elif kind == 'conflict': raw['dailyChart']['candles'].append({'date': '2026-10-21', 'close': 4})
    elif kind == 'invalid-date': raw['dailyChart']['candles'][0]['date'] = '2026-10-99'
    else: raw['dailyChart']['candles'][0]['close'] = 0
    assert query(p)['pair']['status'] == 'unavailable'


def test_extreme_values_serialize_finitely_without_overflow():
    import json
    data = query(Provider([row(pe=1e308), row('000002.SZ', pe=1.7e308), row('000003.SZ', pe=float('inf'))]))
    json.dumps(data, allow_nan=False)
    assert metric(data, 'pe')['sampleCount'] == 2


def test_http_route_envelope_validation_and_readonly_provider_contract():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.stocks.routes import create_router
    p = Provider([row(), row('000002.SZ')])
    app = FastAPI()
    app.include_router(create_router(provider=p))
    with TestClient(app) as client:
        response = client.post('/api/data/stocks/research', json={'symbol': '000001.SZ'})
        assert response.status_code == 200
        assert response.json()['data']['current']['symbol'] == '000001.SZ'
        assert response.json()['meta']['version'] == 'fixed'
        assert client.post('/api/data/stocks/research', json={'symbol': '000001.SZ', 'price': 1}).status_code == 422


def test_amount_comparison_distribution_and_recommendation_never_mix_currencies():
    p = Provider([row(marketCap=100, researchValues={'netCash': 10}),
                  row('000002.SZ', currency='HKD', marketCap=10000, researchValues={'netCash': 999}),
                  row('000003.SZ', marketCap=200, researchValues={'netCash': 20})])
    assert query(p)['peer']['symbol'] == '000003.SZ'
    data = query(p, peerSymbol='000002.SZ')
    for key in ('marketCap', 'netCash'):
        m = metric(data, key)
        assert m['peerValue'] is None and m['difference'] is None
        assert m['sampleCount'] == 2 and m['industrySampleCount'] == 2
        assert m['percentile'] == 50
        assert '币种' in m['reason'] and 'CNY' in m['reason']
    assert metric(data, 'pb')['sampleCount'] == 3  # 无量纲指标仍可用


def test_unknown_currency_does_not_produce_amount_values_or_ranked_peer():
    p = Provider([row(currency=None, researchValues={'netCash': 10}), row('000002.SZ')])
    data = query(p)
    assert data['peer'] is None
    for key in ('marketCap', 'netCash'):
        m = metric(data, key)
        assert m['value'] is None and m['sampleCount'] == 0 and m['percentile'] is None
