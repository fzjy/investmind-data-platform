from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest

from app.db.models import Security
from app.stocks.derived.pipeline import DerivedReport
from app.jobs.build_and_publish import BuildAndPublishJob
from app.marts.contracts import MartRows, QualityContext
from app.marts.publication import SnapshotPublisher
from app.marts.quality import QualityGate
from app.stocks.marts.stocks import ScreenerMartBuilder

from backend.tests.marts.test_screener_builder import _core_snapshot


@pytest.mark.parametrize("failure", ["rows", "field", "roic", "revenueGrowth"])
def test_last_successful_publication_detects_consistent_but_degraded_candidate(pg_factory, failure):
    snapshot = _core_snapshot()
    extra = Security(ts_code="000001.SZ", symbol="000001", name="sample", exchange="SZSE")
    snapshot = replace(snapshot, securities=(*snapshot.securities, extra))
    candidate = ScreenerMartBuilder().build(snapshot, "test")
    rows = [deepcopy(candidate.payload["stocks"][0]),
            {**deepcopy(candidate.payload["stocks"][0]), "symbol": "000001.SZ"}]
    if failure in {"roic", "revenueGrowth"}:
        for row in rows:
            row[failure] = 10
    original = replace(candidate, payload={**candidate.payload, "stocks": rows}, rows=MartRows(screener=tuple(rows)))
    publisher = SnapshotPublisher(pg_factory)
    publisher.publish_many((original,), "good-baseline", ())
    if failure != "rows":
        rows = deepcopy(rows)
        for row in rows:
            row["pe" if failure == "field" else failure] = None
    else:
        rows = rows[:1]
    damaged = replace(candidate, payload={**candidate.payload, "stocks": rows}, rows=MartRows(screener=tuple(rows)))
    job = BuildAndPublishJob(None, None, None, QualityGate(), publisher, None)
    context = job._quality_context(snapshot, (), DerivedReport((), 0, 0, 0, 0))
    apply_baseline = getattr(job, "_baseline_context", None)
    assert apply_baseline is not None
    context = apply_baseline(context, snapshot)
    quality = {item.name: item for item in QualityGate().evaluate((damaged,), context)}
    name = "screener-record-count-stable" if failure == "rows" else "screener-field-coverage-stable"
    assert name in quality and not quality[name].passed and quality[name].severity == "blocking"
    assert publisher.current("screener").version == "good-baseline"


def test_first_release_and_historical_release_do_not_have_false_baseline(pg_factory):
    snapshot = _core_snapshot()
    publisher = SnapshotPublisher(pg_factory)
    job = BuildAndPublishJob(None, None, None, QualityGate(), publisher, None)
    context = job._quality_context(snapshot, (), DerivedReport((), 0, 0, 0, 0))
    apply_baseline = getattr(job, "_baseline_context", None)
    assert apply_baseline is not None
    assert apply_baseline(context, snapshot).baseline_screener_rows is None
    candidate = ScreenerMartBuilder().build(snapshot, "test")
    publisher.publish_many((candidate,), "good", ())
    earlier = replace(snapshot, as_of=date(2026, 8, 23))
    assert apply_baseline(context, earlier).baseline_screener_rows is None


def test_retired_stock_is_not_an_anomalous_missing_row(pg_factory):
    snapshot = _core_snapshot()
    publisher = SnapshotPublisher(pg_factory)
    candidate = ScreenerMartBuilder().build(snapshot, "test")
    publisher.publish_many((candidate,), "good", ())
    job = BuildAndPublishJob(None, None, None, QualityGate(), publisher, None)
    snapshot = replace(snapshot, securities=(), daily_prices=(), daily_basics=())
    context = job._quality_context(snapshot, (), DerivedReport((), 0, 0, 0, 0))
    apply_baseline = getattr(job, "_baseline_context", None)
    assert apply_baseline is not None
    context = apply_baseline(context, snapshot)
    empty = ScreenerMartBuilder().build(snapshot, "test")
    results = QualityGate().evaluate((empty,), context)
    assert all(item.passed for item in results if item.severity == "blocking")


@pytest.mark.parametrize("limit,passed", [(0.2, True), (0.200001, True), (0.199999, False)])
def test_field_drop_boundary_uses_exact_counts_not_float_subtraction(limit, passed):
    candidate = ScreenerMartBuilder().build(_core_snapshot(), "boundary")
    baseline = [{**deepcopy(candidate.payload["stocks"][0]), "symbol": f"{600519 + i:06d}.SH",
                 "pe": 20 if i < 4 else None} for i in range(5)]
    current = deepcopy(baseline)
    current[3]["pe"] = None  # 手算4/5-3/5恰好1/5，不能按二进制浮点大于0.2。
    candidate = replace(candidate, payload={**candidate.payload, "stocks": current},
                        rows=MartRows(screener=tuple(current)))
    context = QualityContext(5, (), 0, 0, (), baseline_screener_rows=tuple(baseline), max_field_drop_ratio=limit)
    quality = {item.name: item for item in QualityGate().evaluate((candidate,), context)}
    assert quality["screener-field-coverage-stable"].passed is passed
