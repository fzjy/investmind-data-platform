"""UI-017: hand-calculated visible-history contracts; no supplier or business DB."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from app.macro.marts.macro import MacroMartBuilder
from app.macro.marts.macro_quality import evaluate_macro_quality
from backend.tests.marts.test_macro_builder import CATALOG, observation

CUTOFF = datetime(2026, 9, 20, tzinfo=timezone.utc)


class NoRepository:
    def visible_observations(self, *args):
        raise AssertionError('observations must be injected')


def row(series_id, period, value, *, released=None):
    source = CATALOG.require(series_id).sources[0].provider
    return observation(series_id, source, date.fromisoformat(period),
                       released or CUTOFF - timedelta(days=1), str(value))


def build(*rows):
    return MacroMartBuilder(NoRepository(), catalog=CATALOG).build(
        CUTOFF, 'pending', observations=rows)


def derived(insights, series_id):
    return next(item for item in insights.payload['derivedInsights']['series']
                if item['id'] == series_id)


def test_state_contract_is_produced_from_visible_points_and_rejects_tampering():
    from app.marts.dto import validate_product_payload
    from app.macro.state_rules import derive_macro_states
    transmission, insights = build(
        row('cn.money.m1_yoy', '2026-08-01', 4), row('cn.money.m2_yoy', '2026-08-01', 7),
        row('cn.ppi.yoy', '2026-08-01', -1), row('cn.rate.dr007', '2026-09-15', 1.5))
    contract = insights.payload.get('stateInsights')
    assert contract is not None, 'builder must produce stateInsights'
    assert contract['cards']['money'] == 'loose'
    assert contract['thermometer']['state'] == 'cold'
    assert contract['thermometer']['availableInputCount'] == 2
    assert validate_product_payload('macro-insights', insights.payload) == ()
    for mutation in ('score', 'value', 'bool', 'date', 'version'):
        payload = deepcopy(insights.payload)
        if mutation == 'score':
            payload['stateInsights']['thermometer']['score'] = 75
        elif mutation == 'value':
            payload['stateInsights']['signals']['scissors']['value'] = 2
        elif mutation == 'bool':
            payload['stateInsights']['thermometer']['availableInputCount'] = True
        elif mutation == 'date':
            payload['stateInsights']['signals']['price']['period'] = '2026-07-01'
        else:
            payload['stateInsights']['algorithmVersion'] = 'other'
        assert 'stateInsights' in validate_product_payload('macro-insights', payload)
        from dataclasses import replace
        candidate = replace(insights, payload=payload)
        checks = evaluate_macro_quality(CATALOG, (transmission, candidate), ())
        assert not next(item for item in checks if item.name == 'macro-derived-valid').passed
    legacy = deepcopy(insights.payload)
    legacy.pop('stateInsights')
    assert 'stateInsights' not in validate_product_payload('macro-insights', legacy)
    assert derive_macro_states({}, [])['thermometer']['state'] == 'missing'


def test_spread_card_contains_all_common_history_not_only_latest_point():
    transmission, _ = build(
        row('cn.ppi.yoy', '2026-07-01', 3), row('cn.ppi.yoy', '2026-08-01', 4),
        row('cn.cpi.yoy', '2026-07-01', 1), row('cn.cpi.yoy', '2026-08-01', 2))
    card = next(c for c in transmission.payload['analysis']['cards'] if c['id'] == 'cn.ppi_cpi_spread')
    assert [(p['quarter'], p['primary'], p['secondary'], p['result']) for p in card['points']] == [
        ('2026-07-01', 3, 1, 2), ('2026-08-01', 4, 2, 2)]
    assert card['meta']['frequency'] == 'monthly'
    assert card['meta']['resultUnit'] == '百分点'


def test_latest_mismatch_keeps_common_history_and_explicit_unresolved_boundary():
    transmission, insights = build(
        row('cn.rate.govbond_10y', '2026-09-15', 1.7),
        row('cn.rate.govbond_10y', '2026-09-16', 1.8),
        row('cn.rate.dr007', '2026-09-15', 1.2))
    cards = {c['id']: c for c in transmission.payload['analysis']['cards']}
    assert cards['cn.govbond_dr007_spread']['points'][0]['result'] == 0.5
    result = derived(insights, 'cn.govbond_dr007_spread')
    assert result['latestPeriod'] == '2026-09-15'
    assert result['inputLatestPeriods'] == {'cn.rate.govbond_10y': '2026-09-16', 'cn.rate.dr007': '2026-09-15'}
    assert result['status'] == 'lagged' and result['reason'] == 'latest-period-mismatch'
    assert any(m.get('reason') == 'derived-period-mismatch' and m.get('card') == result['id']
               for m in insights.payload['missingData'])


def test_money_direction_basis_points_and_true_zero_are_not_mixed():
    _, insights = build(
        row('cn.money.m1_yoy', '2026-08-01', 4), row('cn.money.m2_yoy', '2026-08-01', 7.5),
        row('cn.rate.govbond_10y', '2026-09-15', 1.66), row('us.rate.treasury_10y', '2026-09-15', 4.25),
        row('cn.ppi.yoy', '2026-08-01', 0), row('cn.cpi.yoy', '2026-08-01', 0))
    assert derived(insights, 'cn.money.m1_minus_m2_yoy')['points'] == [{'month': '2026-08-01', 'value': -3.5}]
    assert derived(insights, 'cn.m2_m1_spread')['points'][0]['value'] == 3.5
    spread = derived(insights, 'cn.us.rate_spread')
    assert spread['unit'] == 'bp' and spread['points'][0]['value'] == -259
    assert derived(insights, 'cn.ppi_cpi_spread')['points'][0]['value'] == 0
    assert len(insights.payload['trendDefinitions']) == 20
    assert len(insights.payload['seriesStatus']) == 20


def test_disjoint_periods_do_not_forward_fill_or_create_a_value():
    _, insights = build(row('cn.money.m1_yoy', '2026-07-01', 4), row('cn.money.m2_yoy', '2026-08-01', 7))
    result = derived(insights, 'cn.money.m1_minus_m2_yoy')
    assert result['points'] == [] and result['latestPeriod'] is None
    assert result['status'] == 'missing' and result['reason'] == 'no-common-period'


def test_compass_m3_requires_three_consecutive_months_and_preserves_cross_year_gap():
    inputs = []
    for period, value in [('2025-11-01', 1), ('2025-12-01', 2),
                          ('2026-02-01', 3), ('2026-03-01', 4), ('2026-04-01', 5)]:
        inputs.extend((row('cn.industry.value_added_yoy', period, value), row('cn.ppi.yoy', period, value * 2)))
    _, insights = build(*inputs)
    compass = insights.payload['derivedInsights']['compass']
    assert compass['points'] == [{'month': '2026-04', 'iva': 4, 'ppi': 8}]
    assert compass['windowMonths'] == 3 and compass['displayMonths'] == 12
    assert compass['expectedPoints'] == 12 and compass['status'] == 'partial'
    assert '2026-01' in compass['missingPeriods'] and '2026-03' in compass['missingPeriods']
    assert insights.payload['compassPoints'] == compass['points']


def test_compass_window_is_last_twelve_calendar_months_not_twelve_older_valid_points():
    inputs = []
    for index in range(15):
        period = date(2025 + index // 12, index % 12 + 1, 1).isoformat()
        inputs.extend((row('cn.industry.value_added_yoy', period, index + 1), row('cn.ppi.yoy', period, -(index + 1))))
    _, insights = build(*inputs)
    compass = insights.payload['derivedInsights']['compass']
    assert len(compass['points']) == 12 and compass['status'] == 'complete'
    assert compass['points'][0] == {'month': '2025-04', 'iva': 3, 'ppi': -3}
    assert compass['points'][-1] == {'month': '2026-03', 'iva': 14, 'ppi': -14}


def test_future_revision_does_not_leak_into_derived_series_or_mutate_rows():
    first = row('cn.money.m1_yoy', '2026-08-01', 4)
    second = row('cn.money.m2_yoy', '2026-08-01', 7)
    future = row('cn.money.m1_yoy', '2026-08-01', 99, released=CUTOFF + timedelta(seconds=1))
    _, insights = build(first, second, future)
    assert derived(insights, 'cn.money.m1_minus_m2_yoy')['points'][0]['value'] == -3
    assert str(first.value) == '4' and str(second.value) == '7'


def test_structural_iva_january_february_exclusions_do_not_look_like_a_collection_failure():
    inputs = []
    for index in range(15):
        period = date(2025 + index // 12, index % 12 + 1, 1)
        inputs.append(row('cn.ppi.yoy', period.isoformat(), 3))
        if period.month not in (1, 2):
            inputs.append(row('cn.industry.value_added_yoy', period.isoformat(), 6))
    _, insights = build(*inputs)
    compass = insights.payload['derivedInsights']['compass']
    assert compass['expectedPoints'] == 12 and compass['eligiblePoints'] == 8
    assert len(compass['points']) == 8 and compass['status'] == 'complete'
    assert compass['structuralMissingPeriods'] == ['2025-04', '2026-01', '2026-02', '2026-03']
    assert compass['unexpectedMissingPeriods'] == []
    assert compass['latestPeriod'] == '2025-12'
    assert compass['stale'] is True  # 当前已到三月，但最后可画M3点仍在十二月。


def test_quality_binds_original_and_derived_history_to_selected_core_even_when_both_are_tampered():
    from app.macro.derivations import derive_macro_insights
    inputs = (row('cn.money.m1_yoy', '2026-08-01', 4), row('cn.money.m2_yoy', '2026-08-01', 7))
    candidates = build(*inputs)
    insights = candidates[1].payload
    trend = next(t for t in insights['trendDefinitions'] if t['id'] == 'cn.money.m1_yoy')
    trend['series'][0]['values'][0]['value'] = 40
    insights['derivedInsights'] = derive_macro_insights(
        {t['id']: t['series'][0]['values'] for t in insights['trendDefinitions']}, insights['seriesStatus'])
    card = next(c for c in candidates[0].payload['analysis']['cards'] if c['id'] == 'cn.m2_m1_spread')
    card['points'][0].update(secondary=40, result=-33, value=-33)
    checks = evaluate_macro_quality(CATALOG, candidates, (), observations=inputs)
    assert not next(c for c in checks if c.name == 'macro-derived-valid').passed


def test_dto_allows_legacy_missing_field_but_rejects_present_invalid_contract():
    from app.marts.dto import validate_product_payload
    _, insights = build()
    legacy = deepcopy(insights.payload)
    legacy.pop('derivedInsights')
    assert validate_product_payload('macro-insights', legacy) == ()
    insights.payload['derivedInsights']['schemaVersion'] = 'unknown'
    assert 'derivedInsights' in validate_product_payload('macro-insights', insights.payload)


def test_noncanonical_monthly_point_does_not_crash_unrelated_eligible_derivations():
    _, insights = build(row('cn.industry.value_added_yoy', '2026-08-15', 4), row('cn.ppi.yoy', '2026-08-01', 2),
                        row('cn.money.m1_yoy', '2026-08-01', 4), row('cn.money.m2_yoy', '2026-08-01', 7))
    assert derived(insights, 'cn.money.m1_minus_m2_yoy')['points'][0]['value'] == -3
    assert insights.payload['derivedInsights']['compass']['points'] == []
    assert insights.payload['derivedInsights']['compass']['excludedInputPeriods'] == [
        {'seriesId': 'cn.industry.value_added_yoy', 'period': '2026-08-15', 'reason': 'NON_CANONICAL_MONTHLY_PERIOD'}]


def test_explicit_rebuild_defaults_to_reuse_without_collecting_or_repromoting(monkeypatch):
    from app.cli import main as cli
    from backend.tests.jobs.test_macro_publish import setup, AS_OF
    from sqlalchemy import select, func
    from app.db.models import PageSnapshot
    engine, factory, _, _, publisher, _ = setup(20)
    dispose = engine.dispose

    class Clock(datetime):
        ticks = 0

        @classmethod
        def now(cls, tz=None):
            result = AS_OF + timedelta(seconds=cls.ticks)
            cls.ticks += 1
            return result.astimezone(tz) if tz else result

    monkeypatch.setattr(cli, 'datetime', Clock)
    monkeypatch.setattr(cli, 'create_engine_and_session', lambda url: (engine, factory))
    monkeypatch.setattr(cli.MacroSeriesCatalog, 'load', lambda *args: CATALOG)
    monkeypatch.setattr(engine, 'dispose', lambda: None)
    try:
        first = cli._run_macro_rebuild(cli.Settings(database_url='sqlite://'))
        second = cli._run_macro_rebuild(cli.Settings(database_url='sqlite://'))
        assert first.status == second.status == 'completed'
        assert first.version == second.version and second.promoted_products == ()
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(PageSnapshot)) == 2
        assert {publisher.current(product).version for product in first.products} == {first.version}
    finally:
        dispose()


@pytest.mark.parametrize('damage', ['duplicate', 'cutoff', 'status', 'boolean'])
def test_publish_gate_rejects_forged_display_metadata_and_bool_numeric_equality(damage):
    inputs = tuple(row(item.id, '2026-08-01', 2 if item.id == 'cn.ppi.yoy' else 1) for item in CATALOG.series)
    inputs += (row('cn.rate.govbond_10y', '2026-09-16', 2),)
    candidates = build(*inputs)
    assert all(q.passed for q in evaluate_macro_quality(CATALOG, candidates, (), observations=inputs) if q.severity == 'blocking')
    cards = candidates[0].payload['analysis']['cards']
    if damage == 'duplicate':
        forged = deepcopy(cards[0])
        forged['points'][0].update(primary=999, secondary=998)
        cards.insert(0, forged)
    elif damage == 'cutoff':
        cards[0]['meta']['latestPeriod'] = '2099-01-01'
    elif damage == 'status':
        card = next(c for c in cards if c['id'] == 'cn.govbond_dr007_spread')
        assert card['meta']['status'] == 'lagged'
        card['meta']['status'] = 'current'
    else:
        result = derived(candidates[1], 'cn.ppi_cpi_spread')
        assert result['points'][0]['value'] == 1
        result['points'][0]['value'] = True
    assert not all(q.passed for q in evaluate_macro_quality(CATALOG, candidates, (), observations=inputs) if q.severity == 'blocking')


@pytest.mark.parametrize('damage', ['value', 'unit', 'id', 'compass', 'schema'])
def test_quality_rejects_tampered_derived_contract(damage):
    candidates = build(row('cn.money.m1_yoy', '2026-08-01', 4), row('cn.money.m2_yoy', '2026-08-01', 7))
    assert next(c for c in evaluate_macro_quality(CATALOG, candidates, ()) if c.name == 'macro-derived-valid').passed
    insights = candidates[1]
    before = deepcopy(insights.payload['derivedInsights'])
    target = derived(insights, 'cn.money.m1_minus_m2_yoy')
    if damage == 'value': target['points'][0]['value'] = 99
    elif damage == 'unit': target['unit'] = 'bp'
    elif damage == 'id': target['id'] = 'bogus'
    elif damage == 'compass': insights.payload['compassPoints'] = [{'month': '2026-08', 'iva': 50, 'ppi': 30}]
    else: insights.payload['derivedInsights']['schemaVersion'] = 'bogus'
    check = next(c for c in evaluate_macro_quality(CATALOG, candidates, ()) if c.name == 'macro-derived-valid')
    assert not check.passed and check.severity == 'blocking'
    assert before['schemaVersion'] == '1.0'
