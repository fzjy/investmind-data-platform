"""扩展计划：独立输入、周期边界、身份隔离与拒绝非法配置。"""
from copy import deepcopy
from datetime import datetime
import json

import pytest

from app.macro.extensions.config import ExtensionError, load_extension_config
from app.macro.extensions.plan import build_extension_plan, validate_extension_plan


def payload():
    def group(frequency, lookback, quote, rows, time):
        return {'collectionEnabled': False, 'schedule': {'frequency': frequency,
            'weekday': 0, 'time': time, 'timezone': 'Asia/Shanghai'},
            'lookbackPeriods': lookback, 'quoteSide': quote,
            'limits': {'maxHttpPerRun': 1, 'maxHttpPerPeriod': 1,
                'requestsPerMinute': 10, 'requestTimeoutSeconds': 20,
                'maxRetries': 0, 'maxResponseBytes': 8388608, 'maxResponseRows': rows}}
    return {'schemaVersion': 1, 'groups': {
        'usdcnh': group('daily', 90, 'bid_close', 366, '19:00'),
        'nominalGdp': group('weekly', 20, None, 64, '09:00')}}


def config(tmp_path, value=None):
    file = tmp_path / 'extensions.json'
    file.write_text(json.dumps(payload() if value is None else value), encoding='utf-8')
    return load_extension_config(file)


def plan(tmp_path, group='usdcnh', at='2026-10-22T20:00:00+08:00', value=None):
    return build_extension_plan(group, config(tmp_path, value), datetime.fromisoformat(at))


def test_default_closed_groups_and_original_catalog_unchanged():
    from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
    before = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).catalog_hash
    value = load_extension_config()
    # 四个独立官方组已纳入唯一扩展配置；仍显式闭合，不能接受任意新组。
    assert {'usdcnh', 'nominalGdp'} <= set(value.groups) <= {
        'usdcnh', 'nominalGdp', 'vix', 'governmentBonds',
        'householdCredit', 'tier1Housing', 'specialBonds', 'propertyLoans', 'marginBalances', 'aShareFloatMv'}
    # 开关是部署控制，不应把C1当时false状态冻结成未来用户配置要求。
    assert all(type(group.collectionEnabled) is bool for group in value.groups.values())
    assert MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).catalog_hash == before


def test_daily_cycle_uses_due_time_and_gmt_date_not_check_time(tmp_path):
    first = plan(tmp_path)
    assert first['scheduledAt'] == '2026-10-22T19:00:00+08:00'
    assert first['endPeriod'] == '2026-10-21'
    assert first['startPeriod'] == '2026-07-24'
    assert first == plan(tmp_path, at='2026-10-23T18:59:59+08:00')
    assert first['fingerprint'] != plan(tmp_path, at='2026-10-23T19:00:00+08:00')['fingerprint']
    assert first['query']['params']['ts_code'] == 'USDCNH.FXCM'
    assert first['periodTimezone'] == 'UTC' and first['unit'] == 'CNH/USD'


def test_weekly_boundary_and_quarter_cross_year(tmp_path):
    p = plan(tmp_path, 'nominalGdp', '2026-01-05T08:59:59+08:00')
    assert p['scheduledAt'].startswith('2025-12-29T09:00')
    assert p['endPeriod'] == '2025Q3' and p['startPeriod'] == '2020Q4'
    p = plan(tmp_path, 'nominalGdp', '2026-01-05T09:00:00+08:00')
    assert p['endPeriod'] == '2025Q4' and p['startPeriod'] == '2021Q1'
    assert p == plan(tmp_path, 'nominalGdp', '2026-01-11T23:59:59+08:00')


def test_gmt_leap_day_and_nonexistent_dst_clock(tmp_path):
    value = payload()
    value['groups']['usdcnh']['lookbackPeriods'] = 2
    p = plan(tmp_path, at='2024-03-01T20:00:00+08:00', value=value)
    assert p['startPeriod'] == '2024-02-28' and p['endPeriod'] == '2024-02-29'
    value['groups']['usdcnh']['schedule'].update(time='02:30', timezone='America/New_York')
    with pytest.raises(ExtensionError):
        plan(tmp_path, at='2026-03-08T10:00:00-04:00', value=value)


def test_dst_fall_back_does_not_move_plan_backwards(tmp_path):
    value = payload()
    value['groups']['usdcnh']['schedule'].update(time='01:30', timezone='America/New_York')
    first = plan(tmp_path, at='2026-11-01T01:50:00-04:00', value=value)
    second = plan(tmp_path, at='2026-11-01T01:20:00-05:00', value=value)
    assert first == second
    assert second['scheduledAt'] == '2026-11-01T01:30:00-04:00'


def test_deep_json_has_only_safe_config_error(tmp_path):
    file = tmp_path / 'deep.json'
    file.write_text('[' * 30000 + '0' + ']' * 30000, encoding='utf-8')
    with pytest.raises(ExtensionError, match='^EXTENSION_CONFIG_INVALID$'):
        load_extension_config(file)


def test_other_group_and_control_switch_do_not_change_identity(tmp_path):
    first = plan(tmp_path)
    value = payload()
    value['groups']['nominalGdp']['lookbackPeriods'] = 8
    value['groups']['usdcnh']['collectionEnabled'] = True
    assert first == plan(tmp_path, value=value)
    value['groups']['usdcnh']['quoteSide'] = 'ask_close'
    assert first['fingerprint'] != plan(tmp_path, value=value)['fingerprint']


@pytest.mark.parametrize('key,value', [('lookbackPeriods', True), ('lookbackPeriods', 0),
    ('lookbackPeriods', 367), ('quoteSide', 'close'), ('token', 'private'),
    ('collectionEnabled', 1)])
def test_bad_group_values_are_safe(tmp_path, key, value):
    raw = payload()
    raw['groups']['usdcnh'][key] = value
    with pytest.raises(ExtensionError, match='^EXTENSION_CONFIG_INVALID$'):
        config(tmp_path, raw)


@pytest.mark.parametrize('section,key,value', [('schedule', 'timezone', 'not/a-zone'),
    ('schedule', 'time', '25:00'), ('schedule', 'weekday', True),
    ('limits', 'maxHttpPerRun', 2), ('limits', 'maxRetries', 1),
    ('limits', 'maxResponseRows', 367), ('limits', 'requestTimeoutSeconds', float('nan'))])
def test_bad_nested_limits(tmp_path, section, key, value):
    raw = payload()
    raw['groups']['usdcnh'][section][key] = value
    with pytest.raises(ExtensionError):
        config(tmp_path, raw)


@pytest.mark.parametrize('raw', ['{"schemaVersion":1,"schemaVersion":1}', '{"secret":"do-not-echo"', '[]'])
def test_duplicate_keys_and_bad_json(tmp_path, raw):
    file = tmp_path / 'bad.json'
    file.write_text(raw, encoding='utf-8')
    with pytest.raises(ExtensionError, match='^EXTENSION_CONFIG_INVALID$'):
        load_extension_config(file)


def test_naive_unknown_and_tampered_plan_are_refused(tmp_path):
    c = config(tmp_path)
    with pytest.raises(ExtensionError):
        build_extension_plan('usdcnh', c, datetime(2026, 1, 1))
    with pytest.raises(ExtensionError):
        build_extension_plan('secret-source', c, datetime.fromisoformat('2026-10-22T00:00:00Z'))
    p = plan(tmp_path)
    validate_extension_plan(p)
    for key, value in [('query', {'api': 'secret', 'params': {}, 'fields': []}),
        ('unit', 'CNY/USD'), ('fingerprint', 'bad'), ('schemaVersion', True),
        ('scheduledAt', '2026-10-22T19:01:00+08:00')]:
        changed = deepcopy(p)
        changed[key] = value
        with pytest.raises(ExtensionError):
            validate_extension_plan(changed)
