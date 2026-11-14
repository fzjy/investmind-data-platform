from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models import MacroObservation
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.marts.macro import MacroMartBuilder
from app.macro.marts.macro_quality import evaluate_macro_quality


from backend.tests.macro.controlled_catalog import controlled_catalog
CATALOG = controlled_catalog()
AS_OF = datetime(2026, 9, 20, 10, tzinfo=UTC)


class NoRepository:
    def visible_observations(self, *args: object) -> tuple[MacroObservation, ...]:
        raise AssertionError("injected observations must not query")


def candidates_for(ids: set[str]):
    rows = tuple(
        MacroObservation(source=item.sources[0].provider if item.sources else "fixture",
                         series_id=item.id, name=item.name, region=item.region,
                         frequency=item.frequency, unit=item.unit,
                         seasonal_adjustment=item.seasonal_adjustment,
                         observed_at=date(2026, 8, 1), released_at=AS_OF - timedelta(days=1),
                         revision="initial", value=Decimal("1"), source_url="https://example.com")
        for item in CATALOG.series if item.id in ids
    )
    return MacroMartBuilder(NoRepository(), catalog=CATALOG).build(AS_OF, "pending", observations=rows)


def results_for(ids: set[str]):
    return {item.name: item for item in evaluate_macro_quality(CATALOG, candidates_for(ids), ())}


def test_full_coverage_passes_all_blocking_gates() -> None:
    results = results_for({item.id for item in CATALOG.series})
    assert all(item.passed for item in results.values() if item.severity == "blocking")


def test_sixteen_passes_with_missing_advisory_and_fifteen_blocks() -> None:
    ids = {item.id for item in CATALOG.series} - {
        "cn.cpi.core_yoy", "cn.pmi.manufacturing", "cn.money.m1_yoy", "us.cpi.core_yoy",
    }
    results = results_for(ids)
    assert all(item.passed for item in results.values() if item.severity == "blocking")
    assert results["macro-degradation"].severity == "advisory"
    assert not results["macro-degradation"].passed
    assert not results_for(ids - {"cn.gdp.real_yoy"})["macro-coverage-at-least-80"].passed


@pytest.mark.parametrize("excluded", ["CN", "US"])
def test_each_region_all_missing_blocks(excluded: str) -> None:
    ids = {item.id for item in CATALOG.series if item.region != excluded}
    assert not results_for(ids)["macro-regions-covered"].passed


@pytest.mark.parametrize("excluded", ["growth", "inflation", "money_credit", "labor", "rates_fx"])
def test_each_category_all_missing_blocks(excluded: str) -> None:
    ids = {item.id for item in CATALOG.series if item.category != excluded}
    assert not results_for(ids)["macro-categories-covered"].passed


def test_duplicate_status_identity_blocks() -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    candidates[0].payload["seriesStatus"][1]["id"] = candidates[0].payload["seriesStatus"][0]["id"]
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, ())}
    assert not results["macro-series-identity-unique"].passed


def test_duplicate_source_observation_identity_is_a_blocking_quality_check() -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    row = MacroObservation(
        source="nbs", series_id="cn.cpi.yoy", observed_at=date(2026, 8, 1),
        released_at=AS_OF - timedelta(days=1), revision="v1", value=Decimal("1"),
    )
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, (), observations=(row, row))}
    check = results["macro-observation-identity-unique"]
    assert check.severity == "blocking" and not check.passed


@pytest.mark.parametrize("unit,value", [("wrong", Decimal("1")), ("%", Decimal("NaN"))])
def test_invalid_raw_observation_blocks_values(unit: str, value: Decimal) -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    definition = CATALOG.require("cn.cpi.yoy")
    row = MacroObservation(
        source=definition.sources[0].provider, series_id=definition.id,
        region=definition.region, frequency=definition.frequency, unit=unit,
        observed_at=date(2026, 8, 1), released_at=AS_OF - timedelta(days=1),
        revision="v1", value=value,
    )
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, (), observations=(row,))}
    assert not results["macro-values-valid"].passed


def test_malformed_sandbox_structure_returns_blocking_results() -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    candidates[1].payload["sandboxIndicators"] = "invalid"
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, ())}
    assert not results["macro-structure-valid"].passed


@pytest.mark.parametrize("field,value", [("unit", "WRONG"), ("observedAt", "bogus"), ("releasedAt", "2020-01-01T00:00:00+00:00")])
def test_invalid_metadata_blocks(field: str, value: str) -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    candidates[0].payload["seriesStatus"][0][field] = value
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, ())}
    assert not results["macro-values-valid"].passed


def test_nonfinite_trend_value_blocks() -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    candidates[1].payload["trendDefinitions"][0]["series"][0]["values"][0]["value"] = float("nan")
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, ())}
    assert not results["macro-values-valid"].passed


def test_nonfinite_sandbox_value_blocks() -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    candidates[1].payload["sandboxIndicators"][0]["values"][0]["value"] = float("inf")
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, ())}
    assert not results["macro-values-valid"].passed


def test_stale_is_advisory_with_usable_value() -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    for candidate in candidates:
        candidate.payload["seriesStatus"][0]["stale"] = True
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, ())}
    assert results["macro-coverage-at-least-80"].passed
    assert not results["macro-degradation"].passed


def test_indicator_unit_mismatch_blocks() -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    candidates[1].payload["sandboxIndicators"][0]["unit"] = "wrong"
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, ())}
    assert not results["macro-values-valid"].passed


def test_release_after_candidate_instant_blocks() -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    candidates[0].payload["seriesStatus"][0]["releasedAt"] = (AS_OF + timedelta(seconds=1)).isoformat()
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, ())}
    assert not results["macro-values-valid"].passed


def test_mismatched_series_status_between_products_blocks_pair() -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    candidates[0].payload["seriesStatus"][0]["sourceUrl"] = "https://different.example"
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, ())}
    assert not results["macro-release-pair-complete"].passed


def test_missing_series_status_field_blocks_structure() -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    for candidate in candidates:
        del candidate.payload["seriesStatus"][0]["sourceUrl"]
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, ())}
    assert not results["macro-structure-valid"].passed


def test_unregistered_actual_source_blocks_values() -> None:
    candidates = candidates_for({item.id for item in CATALOG.series})
    candidates[0].payload["seriesStatus"][0]["actualSource"] = "unregistered"
    results = {item.name: item for item in evaluate_macro_quality(CATALOG, candidates, ())}
    assert not results["macro-values-valid"].passed
