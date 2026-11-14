from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest

from app.db.base import ExternalBase
from app.db.models import MacroObservation
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.marts.dto import validate_product_payload
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.jobs.macro_sync import MacroSeriesOutcome, MacroSourceFailure
from app.connectors.macro.contracts import ProviderError
from app.cli import main as main_module


from backend.tests.macro.controlled_catalog import controlled_catalog
CATALOG = controlled_catalog()


def observation(series_id: str, source: str, observed: date, released: datetime, value: str = "1") -> MacroObservation:
    definition = CATALOG.require(series_id)
    return MacroObservation(source=source, series_id=series_id, name=definition.name,
                            region=definition.region, frequency=definition.frequency,
                            unit=definition.unit, seasonal_adjustment=definition.seasonal_adjustment,
                            observed_at=observed, released_at=released, revision="initial",
                            value=Decimal(value), source_url="https://example.com/source")


def test_repository_prefers_official_same_period_and_keeps_newer_fallback() -> None:
    engine = create_engine("sqlite:///:memory:", execution_options={"schema_translate_map": {"raw": None, "core": None}})
    ExternalBase.metadata.create_all(engine)
    factory = sessionmaker(engine)
    cutoff = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    rows = [
        observation("cn.cpi.yoy", "tushare", date(2026, 7, 1), cutoff - timedelta(days=20)),
        observation("cn.cpi.yoy", "nbs", date(2026, 7, 1), cutoff - timedelta(days=18)),
        observation("cn.cpi.yoy", "tushare", date(2026, 8, 1), cutoff - timedelta(days=2)),
        observation("cn.cpi.yoy", "nbs", date(2026, 7, 1), cutoff + timedelta(hours=1), "99"),
    ]
    with factory.begin() as session:
        session.add_all(rows)
    selected = MacroRepository(factory).visible_observations(
        ["cn.cpi.yoy"], cutoff, {"cn.cpi.yoy": ("nbs", "tushare")}
    )
    assert [(row.observed_at, row.source) for row in selected] == [
        (date(2026, 7, 1), "nbs"), (date(2026, 8, 1), "tushare")
    ]


def test_database_mixed_frequency_periods_are_canonical_dates_not_positional_months() -> None:
    cutoff = datetime(2026, 9, 20, tzinfo=timezone.utc)
    rows = (
        observation("cn.cpi.yoy", "tushare", date(2026, 8, 1), cutoff - timedelta(days=1)),
        observation("us.rate.treasury_10y", "fred", date(2026, 8, 2), cutoff - timedelta(days=1)),
        observation("cn.gdp.real_yoy", "tushare", date(2026, 7, 1), cutoff - timedelta(days=1)),
    )
    _, insights = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG).build(cutoff, "v1", observations=rows)
    assert insights.payload["sandboxMonths"] == ["2026-07-01", "2026-08-01", "2026-08-02"]
    values = {item["id"]: item["values"] for item in insights.payload["sandboxIndicators"]}
    assert values["cn.cpi.yoy"] == [{"month": "2026-08-01", "value": 1.0}]


def test_duplicate_selected_identity_remains_visible_to_quality_gate() -> None:
    cutoff = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    row = observation("cn.cpi.yoy", "tushare", date(2026, 7, 1), cutoff - timedelta(days=1))
    from app.macro.marts.macro import select_catalog_observations
    from app.macro.marts.macro_quality import observation_identity_unique
    selected = select_catalog_observations((row, row), CATALOG, cutoff)
    assert not observation_identity_unique(selected)


def test_future_duplicate_in_injected_rows_is_invisible_to_earlier_build() -> None:
    cutoff = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    visible = observation("cn.cpi.yoy", "tushare", date(2026, 7, 1), cutoff - timedelta(days=1))
    future = observation("cn.cpi.yoy", "tushare", date(2026, 8, 1), cutoff + timedelta(hours=1))
    transmission, _ = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG).build(
        cutoff, "v1", observations=(visible, future, future)
    )
    status = next(item for item in transmission.payload["seriesStatus"] if item["id"] == "cn.cpi.yoy")
    assert status["observedAt"] == "2026-07-01"


def test_injected_rows_use_same_source_and_revision_selector() -> None:
    cutoff = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    rows = (
        observation("cn.cpi.yoy", "tushare", date(2026, 7, 1), cutoff - timedelta(days=2)),
        observation("cn.cpi.yoy", "nbs", date(2026, 7, 1), cutoff - timedelta(days=1)),
        observation("cn.cpi.yoy", "nbs", date(2026, 7, 1), cutoff + timedelta(hours=1), "99"),
    )
    original = CATALOG.require("cn.cpi.yoy")
    official = original.sources[0].model_copy(update={"provider": "nbs", "priority": 1})
    fallback = original.sources[0].model_copy(update={"provider": "tushare", "priority": 2})
    catalog = CATALOG.model_copy(update={"series": tuple(
        item.model_copy(update={"sources": (fallback, official)}) if item.id == original.id else item
        for item in CATALOG.series
    )})
    candidate, _ = MacroMartBuilder(_UnexpectedRepository(), catalog=catalog).build(
        cutoff, "v1", observations=rows
    )
    status = next(item for item in candidate.payload["seriesStatus"] if item["id"] == "cn.cpi.yoy")
    assert status["actualSource"] == "nbs"
    assert candidate.as_of == cutoff


def test_catalog_payload_covers_all_twenty_in_catalog_order() -> None:
    cutoff = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    rows = tuple(observation(item.id, item.sources[0].provider if item.sources else "fixture", date(2026, 8, 1), cutoff - timedelta(days=1)) for item in CATALOG.series)
    transmission, insights = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG).build(
        cutoff, "v1", observations=rows
    )
    ids = [item.id for item in CATALOG.series]
    assert [item["id"] for item in transmission.payload["seriesStatus"]] == ids
    assert [item["id"] for item in insights.payload["seriesStatus"]] == ids
    assert [item["id"] for item in insights.payload["sandboxIndicators"]] == ids
    assert [item["id"] for item in insights.payload["trendDefinitions"]] == ids
    assert {item["id"] for item in insights.payload["sandboxGroups"]} == {"growth", "inflation", "money_credit", "labor", "rates_fx"}
    assert {item["code"] for item in transmission.payload["transmission"]["weather"]} == {"cn.rate.dr007", "cn.rate.govbond_10y", "cn.fx.usdcny", "us.rate.effective_fed_funds", "us.rate.treasury_10y"}
    assert transmission.payload["transmission"]["industries"] == []
    assert {item["id"] for item in transmission.payload["analysis"]["cards"]} >= {"cn.ppi_cpi_spread", "cn.m2_m1_spread"}
    assert {item["id"] for item in transmission.payload["analysis"]["cards"]} >= {"cn.govbond_dr007_spread", "us.treasury_fed_funds_spread"}
    assert validate_product_payload("macro-transmission", transmission.payload) == ()
    assert validate_product_payload("macro-insights", insights.payload) == ()


def test_existing_observation_does_not_inherit_initial_state_missing_reason() -> None:
    from app.db.base import Base
    from app.db.session import create_engine_and_session
    from app.macro.jobs.macro_series_state import MacroSeriesStateRepository
    cutoff = datetime(2026, 9, 30, tzinfo=timezone.utc)
    row = observation("cn.cpi.yoy", "tushare", date(2026, 8, 1), cutoff - timedelta(days=1))
    engine, factory = create_engine_and_session("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        repository = MacroSeriesStateRepository(factory, CATALOG.catalog_version)
        repository.mark_stale(row.series_id, checked_at=cutoff, error=ProviderError("MACRO_HISTORY_GAP", retryable=False))
        states = repository.get_all()
        assert states[0].stale_reason == "missing"
        outcomes = main_module._macro_rebuild_outcomes(CATALOG, (row,), {row.series_id: states[0]}, cutoff)
        target = next(item for item in outcomes if item.series_id == row.series_id)
        assert target.status == "stale" and target.stale_reason == "MACRO_HISTORY_GAP"
        candidates = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG).build(cutoff, "v1", observations=(row,), states=states)
        for candidate in candidates:
            status = next(item for item in candidate.payload["seriesStatus"] if item["id"] == row.series_id)
            assert status["observedAt"] == "2026-08-01"
            assert status["staleReason"] == "MACRO_HISTORY_GAP"
    finally:
        engine.dispose()


def test_all_selected_history_reaches_trends_and_sandbox_without_synthetic_missing_values() -> None:
    cutoff = datetime(2026, 9, 30, 13, tzinfo=timezone.utc)
    rows = tuple(
        observation(item.id, item.sources[0].provider, period, cutoff - timedelta(days=1), str(index))
        for item in CATALOG.series if item.sources[0].provider != "fixture"
        for index, period in enumerate((date(2025, 8, 1), date(2026, 7, 1), date(2026, 8, 1)))
    )
    transmission, insights = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG).build(
        cutoff, "history-test", observations=rows,
    )
    trends = {item["id"]: item["series"][0]["values"] for item in insights.payload["trendDefinitions"]}
    sandbox = {item["id"]: item["values"] for item in insights.payload["sandboxIndicators"]}
    nodes = {item["id"]: item["series"] for mode in transmission.payload["transmission"]["modes"] for item in mode["mainNodes"]}
    for item in CATALOG.series:
        expected = [{"month": row.observed_at.isoformat(), "value": float(row.value)} for row in rows if row.series_id == item.id]
        assert trends[item.id] == sandbox[item.id] == expected
        if expected:
            assert nodes[item.id] == [{"quarter": point["month"], "value": point["value"]} for point in expected]
        else:
            assert item.id not in nodes
    assert len(trends) == len(sandbox) == 20


def test_missing_inputs_omit_derived_cards_and_use_canonical_missing_ids() -> None:
    cutoff = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    transmission, insights = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG).build(
        cutoff, "v1", observations=()
    )
    assert transmission.payload["analysis"]["cards"] == []
    assert [item["source"] for item in transmission.missing_data if item["reason"] == "no-visible-observation"] == [item.id for item in CATALOG.series]
    assert insights.payload["missingSeries"] == [item.id for item in CATALOG.series]
    assert transmission.stale and insights.stale


def test_derived_spread_requires_matching_observation_periods() -> None:
    cutoff = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    rows = (
        observation("cn.cpi.yoy", "tushare", date(2026, 7, 1), cutoff - timedelta(days=2)),
        observation("cn.ppi.yoy", "tushare", date(2026, 8, 1), cutoff - timedelta(days=1)),
    )
    transmission, _ = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG).build(
        cutoff, "v1", observations=rows
    )
    assert "cn.ppi_cpi_spread" not in {item["id"] for item in transmission.payload["analysis"]["cards"]}
    assert {item["source"] for item in transmission.missing_data if item["reason"] == "derived-period-mismatch"} == {
        "cn.cpi.yoy", "cn.ppi.yoy",
    }


def test_missing_rate_inputs_explain_omitted_cn_and_us_spreads() -> None:
    cutoff = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    transmission, _ = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG).build(
        cutoff, "v1", observations=()
    )
    omitted = {item["source"] for item in transmission.missing_data if item["reason"] == "derived-input-missing"}
    assert {"cn.rate.govbond_10y", "cn.rate.dr007", "us.rate.treasury_10y", "us.rate.effective_fed_funds"} <= omitted


def test_stale_outcome_without_stored_state_has_stable_reason() -> None:
    cutoff = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    row = observation("cn.cpi.yoy", "tushare", date(2026, 8, 1), cutoff - timedelta(days=1))
    outcome = MacroSeriesOutcome("cn.cpi.yoy", "stale", "tushare", 0, 0, 0, ())
    transmission, _ = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG).build(
        cutoff, "v1", observations=(row,), states=(outcome,)
    )
    status = next(item for item in transmission.payload["seriesStatus"] if item["id"] == "cn.cpi.yoy")
    assert status["stale"] is True
    assert status["staleReason"] == "source_failed"


@pytest.mark.parametrize("provider_failed,expected_reason", [
    (False, "MACRO_VALUE_EXPIRED"),
    (True, "source_failed"),
])
def test_expired_value_payload_distinguishes_rebuild_from_provider_failure(
    provider_failed, expected_reason,
) -> None:
    cutoff = datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    old_date = (cutoff - timedelta(days=1000)).date()
    row = observation("cn.cpi.yoy", "tushare", old_date, cutoff - timedelta(hours=1))
    if provider_failed:
        outcomes = (MacroSeriesOutcome(
            row.series_id, "stale", row.source, 1, 0, 0,
            (MacroSourceFailure(row.source, ProviderError("SOURCE_FAILED", retryable=False)),),
        ),)
    else:
        outcomes = main_module._macro_rebuild_outcomes(CATALOG, (row,), {}, cutoff)
    candidates = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG).build(
        cutoff, "v1", observations=(row,), states=outcomes,
    )
    for candidate in candidates:
        status = next(item for item in candidate.payload["seriesStatus"] if item["id"] == row.series_id)
        assert status["stale"] is True
        assert status["staleReason"] == expected_reason
        assert status["observedAt"] == old_date.isoformat()


@pytest.mark.parametrize("with_fresh_outcome", [False, True])
def test_daily_observation_expires_without_stored_state(with_fresh_outcome: bool) -> None:
    observed = date(2026, 8, 1)
    row = observation("cn.rate.govbond_10y", "chinamoney", observed, datetime(2026, 8, 2, tzinfo=timezone.utc))
    outcome = MacroSeriesOutcome(row.series_id, "fresh", row.source, 0, 0, 0, ())
    builder = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG)
    states = (outcome,) if with_fresh_outcome else ()

    fresh, _ = builder.build(datetime(2026, 8, 8, 10, tzinfo=timezone.utc), "v1", observations=(row,), states=states)
    expired, insights = builder.build(datetime(2026, 8, 9, 10, tzinfo=timezone.utc), "v2", observations=(row,), states=states)
    fresh_status = next(item for item in fresh.payload["seriesStatus"] if item["id"] == row.series_id)
    expired_status = next(item for item in expired.payload["seriesStatus"] if item["id"] == row.series_id)
    assert fresh_status["stale"] is False
    assert fresh_status["staleReason"] is None
    assert expired_status["stale"] is True
    assert expired_status["staleReason"] == "MACRO_VALUE_EXPIRED"
    assert expired.stale and insights.stale


class _UnexpectedRepository:
    def visible_observations(self, series_ids, as_of):
        raise AssertionError("快照构建期间不应再次查询宏观表")


@pytest.mark.parametrize("cutoff,expired", [
    ("2026-09-29T12:00:00+00:00", False),
    ("2026-11-07T12:00:00+00:00", False),
    ("2026-11-08T12:00:00+00:00", True),
])
@pytest.mark.parametrize("series_id", ["cn.gdp.real_yoy", "us.gdp.real_qoq_annualized"])
def test_quarterly_expiry_is_measured_from_period_end_in_rebuild_and_payload(cutoff, expired, series_id):
    as_of = datetime.fromisoformat(cutoff)
    row = observation(series_id, CATALOG.require(series_id).sources[0].provider,
                      date(2026, 4, 1), datetime(2026, 7, 30, tzinfo=timezone.utc))
    outcomes = main_module._macro_rebuild_outcomes(CATALOG, (row,), {}, as_of)
    target = next(item for item in outcomes if item.series_id == series_id)
    assert target.status == ("stale" if expired else "fresh")
    candidates = MacroMartBuilder(_UnexpectedRepository(), catalog=CATALOG).build(
        as_of, "quarter-v1", observations=(row,), states=outcomes)
    for candidate in candidates:
        status = next(item for item in candidate.payload["seriesStatus"] if item["id"] == series_id)
        assert status["stale"] is expired
        assert status["observedAt"] == "2026-04-01"


def test_as_of_query_does_not_see_later_revision() -> None:
    engine = create_engine("sqlite:///:memory:", execution_options={"schema_translate_map": {"raw": None, "core": None}})
    ExternalBase.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory.begin() as session:
        session.add_all([observation("cn.cpi.yoy", "tushare", date(2026, 7, 1), datetime(2026, 8, 9, tzinfo=timezone.utc), "0.1"), observation("cn.cpi.yoy", "tushare", date(2026, 7, 1), datetime(2026, 9, 9, tzinfo=timezone.utc), "0.2")])
    rows = MacroRepository(factory).visible_observations(["cn.cpi.yoy"], datetime(2026, 8, 10, tzinfo=timezone.utc))
    assert rows[0].value == Decimal("0.1")

    transmission, _ = MacroMartBuilder(MacroRepository(factory)).build(
        datetime(2026, 8, 10, tzinfo=timezone.utc),
        "v1",
    )
    assert transmission.payload["transmission"]["modes"][1]["mainNodes"][0]["value"] == "0.1000000000%"


def test_empty_macro_data_is_partial_but_keeps_required_keys() -> None:
    engine = create_engine(
        "sqlite:///:memory:",
        execution_options={"schema_translate_map": {"raw": None, "core": None}},
    )
    ExternalBase.metadata.create_all(engine)
    factory = sessionmaker(engine)

    transmission, insights = MacroMartBuilder(MacroRepository(factory)).build(
        datetime(2026, 8, 24, tzinfo=timezone.utc),
        "v1",
    )

    assert transmission.payload["transmission"]["weather"] == []
    assert [mode["id"] for mode in transmission.payload["transmission"]["modes"]] == ["growth", "inflation", "money_credit", "labor", "rates_fx"]
    assert transmission.payload["transmission"]["industries"] == []
    assert transmission.payload["analysis"] == {"cards": []}
    assert insights.payload["quality"] == "partial"
    assert set(insights.payload) >= {
        "compassPoints",
        "sandboxGroups",
        "sandboxIndicators",
        "sandboxMonths",
        "sandboxPresets",
        "statusCards",
        "trendDefinitions",
        "missingData",
    }


def test_snapshot_observations_are_used_without_requerying_repository() -> None:
    rows = (
        MacroObservation(
            source="tushare",
            series_id="cn.cpi.yoy",
            name="居民消费价格指数",
            region="CN",
            frequency="monthly",
            unit="%",
            seasonal_adjustment="unknown",
            observed_at=datetime(2026, 6, 1).date(),
            released_at=datetime(2026, 7, 9, tzinfo=timezone.utc),
            revision="initial",
            value=Decimal("0.1"),
            source_url="https://example.com/cpi-1",
        ),
        MacroObservation(
            source="tushare",
            series_id="cn.cpi.yoy",
            name="居民消费价格指数",
            region="CN",
            frequency="monthly",
            unit="%",
            seasonal_adjustment="unknown",
            observed_at=datetime(2026, 7, 1).date(),
            released_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
            revision="initial",
            value=Decimal("0.2"),
            source_url="https://example.com/cpi-2",
        ),
    )

    transmission, insights = MacroMartBuilder(_UnexpectedRepository()).build(
        datetime(2026, 8, 10, tzinfo=timezone.utc),
        "v1",
        observations=rows,
    )

    assert transmission.payload["transmission"]["modes"][1]["id"] == "inflation"
    assert transmission.payload["transmission"]["modes"][1]["mainNodes"][0]["series"]
    assert insights.payload["statusCards"][0]["id"] == "cn.cpi.yoy"
    assert insights.payload["trendDefinitions"][1]["series"][0]["values"] == [
        {"month": "2026-06-01", "value": 0.1},
        {"month": "2026-07-01", "value": 0.2},
    ]
    assert insights.payload["sandboxIndicators"][1]["values"]
    assert validate_product_payload("macro-transmission", transmission.payload) == ()
    assert validate_product_payload("macro-insights", insights.payload) == ()


def test_macro_dto_validator_reports_nested_type_errors() -> None:
    row = MacroObservation(
        source="tushare",
        series_id="cn.cpi.yoy",
        name="居民消费价格指数",
        region="CN",
        frequency="monthly",
        unit="%",
        seasonal_adjustment="unknown",
        observed_at=datetime(2026, 7, 1).date(),
        released_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        revision="initial",
        value=Decimal("0.2"),
        source_url="https://example.com/cpi",
    )
    transmission, insights = MacroMartBuilder(_UnexpectedRepository()).build(
        datetime(2026, 8, 10, tzinfo=timezone.utc),
        "v1",
        observations=(row,),
    )
    transmission.payload["transmission"]["modes"][1]["mainNodes"][0]["series"][0]["value"] = "0.2"
    del insights.payload["statusCards"][0]["detail"]

    transmission_errors = validate_product_payload(
        "macro-transmission", transmission.payload
    )
    insight_errors = validate_product_payload("macro-insights", insights.payload)

    assert "transmission.modes[1].mainNodes[0].series[0].value" in transmission_errors
    assert "statusCards[0].detail" in insight_errors
