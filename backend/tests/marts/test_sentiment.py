from dataclasses import replace
from datetime import date, datetime, UTC
from decimal import Decimal
import copy
import pytest

from app.db.models import DailyPrice, Security
from app.market.data.catalog import MarketDataCatalog, plan_market_targets
from app.market.marts.market import MarketMartBuilder
from app.marts.dto import validate_product_payload
from backend.tests.marts.test_screener_builder import _core_snapshot

DAY = date(2026, 8, 24)


def snapshot(rows):
    return replace(_core_snapshot(), securities=tuple(Security(ts_code=r.ts_code, name=r.ts_code) for r in rows),
                   daily_prices=tuple(rows), daily_basics=())


def price(code, close='10', high='11', **extra):
    return DailyPrice(ts_code=code, trade_date=DAY, close=Decimal(close), high=Decimal(high),
                      pre_close=Decimal('10'), vol=Decimal('100'), **extra)


def result(rows):
    return MarketMartBuilder().build(snapshot(rows), 'test')[0].payload


def test_threshold_sample_and_contributions():
    rows = [price(str(i), '11', '11') for i in range(75)]
    rows += [price(str(i), '10.1', '11' if i < 191 else '10.1') for i in range(75, 2242)]
    rows += [price(str(i), '9.9', '10') for i in range(2242, 5573)]
    payload = result(rows)
    value = payload['sentimentSnapshot']
    assert value['score'] == 32
    assert value['counts'] == dict(total=5573, rise=2242, thresholdRise=75, thresholdFall=0, touched=191, retreat=116)
    assert value['contributions']['rise'] == pytest.approx(24.1378, abs=.0001)
    assert value['contributions']['thresholdRise'] == pytest.approx(8.0746, abs=.0001)
    assert value['contributions']['retreat'] == 0
    assert value['level'] == '恐慌'
    assert validate_product_payload('market-overview', payload) == ()


def test_zero_touch_has_no_unearned_points():
    value = result([price('a', '10.1', '10.1')])['sentimentSnapshot']
    assert value['score'] is None
    assert value['thresholdRetreatRatio'] is None
    assert value['contributions']['retreat'] is None
    assert value['status'] == 'unobservable'
    assert '未触及' in value['reason']


def test_exact_thresholds_and_missing_coverage():
    rows = [price('a', '10.98', '10.98'), price('b', '9.02', '10.98')]
    value = result(rows)['sentimentSnapshot']
    assert value['counts'] == dict(total=2, rise=1, thresholdRise=1, thresholdFall=1, touched=2, retreat=1)
    assert value['score'] == 60
    rows[1].high = None
    value = result(rows)['sentimentSnapshot']
    assert value['score'] is None and value['status'] == 'partial'
    assert value['coverage'] == dict(expected=2, valid=1, excluded=0, missing=1)


@pytest.mark.parametrize('field,bad', [('pre_close', 0), ('high', 'NaN'), ('close', '-1'), ('vol', None), ('high', '9')])
def test_invalid_inputs_never_become_observed_zero(field, bad):
    row = price('a')
    setattr(row, field, Decimal(bad) if bad is not None else None)
    value = result([row])['sentimentSnapshot']
    assert value['score'] is None and value['coverage']['missing'] == 1


def test_missing_day_and_explicit_ineligible_rows():
    rows = [price('a', '11'), price('b')]
    rows[1].vol = Decimal(0)
    value = result(rows)['sentimentSnapshot']
    assert value['coverage'] == dict(expected=2, valid=1, excluded=1, missing=0)
    assert value['score'] == 100
    rows[1].trade_date = date(2026, 8, 23)
    value = result(rows)['sentimentSnapshot']
    assert value['score'] is None and value['coverage']['missing'] == 1


def test_sentiment_config_does_not_change_collection_fingerprint_or_targets():
    catalog = MarketDataCatalog.load()
    payload = catalog.model_dump(mode='json', by_alias=True)
    assert 'sentiment' in payload
    legacy = copy.deepcopy(payload)
    legacy.pop('sentiment')
    payload['sentiment']['threshold'] = '0.09'
    changed = MarketDataCatalog.model_validate(payload)
    assert changed.fingerprint == catalog.fingerprint == MarketDataCatalog.model_validate(legacy).fingerprint
    instant = datetime(2026, 11, 9, tzinfo=UTC)
    assert plan_market_targets(changed, instant) == plan_market_targets(catalog, instant)
    row = price('a', '10.95', '10.95')
    assert MarketMartBuilder(sentiment=changed.sentiment).build(snapshot([row]), 'v')[0].payload['sentimentSnapshot']['score'] == 100


@pytest.mark.parametrize('key,value', [('threshold', 0), ('riseWeight', '.9'), ('riseNormalization', 0), ('retreatNormalization', 'NaN'), ('threshold', True)])
def test_invalid_sentiment_config_rejected(key, value):
    payload = MarketDataCatalog.load().model_dump(mode='json', by_alias=True)
    payload.setdefault('sentiment', {})[key] = value
    with pytest.raises(ValueError):
        MarketDataCatalog.model_validate(payload)
