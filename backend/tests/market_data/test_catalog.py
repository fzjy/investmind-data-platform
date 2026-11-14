"""配置或时点校验被移除时，这些有限范围/收盘边界断言应失败。"""
import copy
import importlib
from datetime import datetime

import pytest


def catalog_module():
    assert importlib.util.find_spec('app.market.data') is not None, 'market data package missing'
    return importlib.import_module('app.market.data.catalog')


def catalog():
    return catalog_module().MarketDataCatalog.load()


def test_default_catalog_bounds_assets_and_daily_schedule():
    value = catalog()
    assert tuple(a.asset_code for a in value.assets) == (
        '000001.SH', '399001.SZ', '399006.SZ', 'HSI', 'SPX', 'IXIC',
        '510050.SH', '510300.SH', '159338.SZ', '510500.SH', '512100.SH',
        '588000.SH', '588190.SH', '510880.SH', '512890.SH')
    assert [a.api_name for a in value.assets] == ['index_daily'] * 3 + ['index_global'] * 3 + ['fund_daily'] * 9
    assert value.sync.max_requests_per_run == 20 and value.sync.max_retries == 0
    assert value.sync.lookback_days == 14 and value.sync.timeout_seconds == 20
    assert value.schedule.frequency == 'daily' and value.schedule.time.isoformat() == '19:00:00'
    assert value.schedule.timezone == 'Asia/Shanghai' and value.schedule.catchup_on_start is True


@pytest.mark.parametrize('path,bad', [
    (('assets', 0, 'assetCode'), '000001.SZ'),
    (('assets', 0, 'apiName'), 'fund_daily'),
    (('assets', 0, 'market'), 'us'),
    (('sync', 'lookbackDays'), True), (('sync', 'lookbackDays'), 32),
    (('sync', 'maxRequestsPerRun'), 101), (('sync', 'maxRetries'), 1),
    (('sync', 'timeoutSeconds'), 0), (('schemaVersion',), True),
    (('sync', 'pctChangeTolerancePercentPoints'), 'NaN'),
    (('markets', 'cn', 'timezone'), 'Not/AZone'),
    (('markets', 'cn', 'closeTime'), '25:00'),
    (('markets', 'cn', 'publishDelayMinutes'), -1),
    (('schedule', 'time'), '9:00'), (('schedule', 'frequency'), 'weekly'),
    (('enabled',), 'true'),
])
def test_catalog_rejects_unsafe_values(path, bad):
    value = catalog()
    payload = value.model_dump(mode='json', by_alias=True)
    item = payload
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = bad
    with pytest.raises(ValueError):
        type(value).model_validate(payload)


def test_catalog_rejects_duplicate_or_unknown_identity():
    value = catalog()
    payload = value.model_dump(mode='json', by_alias=True)
    payload['assets'][1] = copy.deepcopy(payload['assets'][0])
    with pytest.raises(ValueError):
        type(value).model_validate(payload)
    payload = value.model_dump(mode='json', by_alias=True)
    payload['markets']['other'] = copy.deepcopy(payload['markets']['cn'])
    with pytest.raises(ValueError):
        type(value).model_validate(payload)


@pytest.mark.parametrize('instant,cn,hk,us', [
    ('2026-10-15T15:13:00+00:00', '2026-10-15', '2026-10-15', '2026-10-14'),
    ('2026-01-09T22:30:00+00:00', '2026-01-09', '2026-01-09', '2026-01-08'),
    ('2026-03-09T22:30:00+00:00', '2026-03-09', '2026-03-09', '2026-03-09'),
    ('2026-10-16T08:59:59+00:00', '2026-10-15', '2026-10-15', '2026-10-15'),
    ('2026-10-16T09:00:00+00:00', '2026-10-16', '2026-10-15', '2026-10-15'),
])
def test_market_upper_dates_follow_local_close_and_dst(instant, cn, hk, us):
    module = catalog_module()
    targets = module.plan_market_targets(catalog(), datetime.fromisoformat(instant))
    expected = {'cn': cn, 'hk': hk, 'us': us}
    for target in targets:
        assert target.end_date.isoformat() == expected[target.asset.market]
        assert (target.end_date - target.start_date).days == 13


def test_target_identity_tracks_config_and_rejects_naive_clock():
    module = catalog_module()
    value = catalog()
    payload = value.model_dump(mode='json', by_alias=True)
    before = copy.deepcopy(payload)
    now = datetime.fromisoformat('2026-10-15T15:13:00+00:00')
    first = module.plan_market_targets(value, now)
    assert payload == before
    payload['sync']['maxRequestsPerRun'] = 2
    other = type(value).model_validate(payload)
    assert other.fingerprint != value.fingerprint
    assert module.plan_market_targets(other, now)[0].identity != first[0].identity
    with pytest.raises(ValueError):
        module.plan_market_targets(value, now.replace(tzinfo=None))


def test_full_day_publish_delay_rechecks_the_previous_day_visibility():
    module = catalog_module()
    payload = catalog().model_dump(mode='json',by_alias=True)
    payload['markets']['cn']['publishDelayMinutes'] = 1440
    value = module.MarketDataCatalog.model_validate(payload)
    targets = module.plan_market_targets(value,datetime.fromisoformat('2026-10-16T02:00:00+00:00'))
    assert targets[0].end_date.isoformat() == '2026-10-14'
