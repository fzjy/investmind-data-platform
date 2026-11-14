from datetime import date

import pytest
from sqlalchemy import select

from app.db.models import MetricInput
from app.stocks.derived.inputs import CoreSnapshotRepository
from app.stocks.derived.pipeline import DerivedPipeline
from app.jobs.build_and_publish import BuildAndPublishJob
from app.marts.quality import QualityGate
from backend.tests.jobs.test_build_and_publish_integration import _factory, _seed_complete_core_sample


@pytest.mark.parametrize("failure", ["hash", "name", "table", "key", "duplicate"])
def test_same_lineage_count_cannot_hide_wrong_input_identity(failure):
    factory = _factory()
    _seed_complete_core_sample(factory)
    snapshot = CoreSnapshotRepository(factory).load(date(2026, 8, 24), ("600519.SH",))
    pipeline = DerivedPipeline(factory)
    report = pipeline.run(snapshot, ("600519.SH",))
    assert report.declared_lineage_count == report.persisted_lineage_count
    with factory.begin() as session:
        rows = session.scalars(select(MetricInput).order_by(MetricInput.id)).all()
        first = rows[0]
        if failure == "hash":
            first.content_hash = "0" * 64
        elif failure == "name":
            first.input_name = "not-the-input"
        elif failure == "table":
            first.source_table = "core.wrong"
        elif failure == "key":
            first.source_record_key = "another-record"
        else:
            # 计数不变，把同一公式的另一输入改成重复身份。
            second = next(row for row in rows[1:] if row.metric_value_id == first.metric_value_id)
            second.input_name = first.input_name
    errors_fn = getattr(pipeline, "_lineage_errors", None)
    assert errors_fn is not None, "逐输入核对尚未实现"
    errors = errors_fn(snapshot, ("600519.SH",))
    assert errors
    assert all("600519.SH" in error for error in errors)


def test_real_derived_run_reports_identity_and_gate_consumes_it():
    factory = _factory()
    _seed_complete_core_sample(factory)
    snapshot = CoreSnapshotRepository(factory).load(date(2026, 8, 24), ("600519.SH",))
    report = DerivedPipeline(factory).run(snapshot, ("600519.SH",))
    assert getattr(report, "lineage_errors", None) == ()
    context = BuildAndPublishJob._quality_context(snapshot, ("600519.SH",), report)
    results = {item.name: item for item in QualityGate().evaluate((), context)}
    assert results["lineage-inputs-match"].passed


def test_legitimate_missing_input_context_still_has_verifiable_identity():
    from app.db.models import Security
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="sample", data={}))
    snapshot = CoreSnapshotRepository(factory).load(date(2026, 8, 24), ("600519.SH",))
    report = DerivedPipeline(factory).run(snapshot, ("600519.SH",))
    assert getattr(report, "lineage_errors", None) == ()
