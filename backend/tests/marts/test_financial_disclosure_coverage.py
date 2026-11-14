"""真实Repository过滤财务数据后，质量诊断仍保留缺披露证据。"""
from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import select

from app.db.models import BalanceSheet, CashflowStatement, FinancialIndicator, IncomeStatement, QualityCheckResult, Security
from app.stocks.derived.inputs import CoreSnapshotRepository, FinancialPoint
from app.jobs.build_and_publish import BuildAndPublishOptions
from app.marts.source_quality import evaluate_source_quality
from backend.tests.derived.test_inputs import _factory
from backend.tests.jobs.test_build_and_publish_integration import (
    _factory as publication_factory, _real_build_and_publish_job, _seed_complete_core_sample,
)


AS_OF = date(2026, 8, 24)


def coverage(snapshot):
    return next(result for result in evaluate_source_quality(snapshot)
                if result.name == "source-financial-coverage")


@pytest.mark.parametrize("model", [IncomeStatement, BalanceSheet, CashflowStatement, FinancialIndicator])
def test_filtered_undisclosed_financial_row_is_reported_without_becoming_an_input(model):
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="sample", status="L", data={}))
        session.add(model(ts_code="600519.SH", end_date=date(2025, 12, 31), report_type="1",
                          data={"revenue": 999999, "n_income": 999999}))
    snapshot = CoreSnapshotRepository(factory).load(AS_OF)
    result = coverage(snapshot)
    assert result.details["missingDisclosureCount"] == 1
    assert not result.passed and result.severity == "advisory"
    assert snapshot.financial_points == ()
    assert snapshot.security_inputs["600519.SH"].values["revenue"] is None
    assert snapshot.security_inputs["600519.SH"].values["parent_profit_ttm"] is None


def test_disclosure_count_respects_requested_symbols_and_historical_report_cutoff():
    factory = _factory()
    with factory.begin() as session:
        session.add_all([Security(ts_code=code, symbol=code[:6], name="sample", status="L", data={})
                         for code in ("600519.SH", "000001.SZ")])
        session.add_all([
            IncomeStatement(ts_code="600519.SH", end_date=date(2025, 12, 31), data={}),
            IncomeStatement(ts_code="600519.SH", end_date=date(2026, 12, 31), data={}),
            IncomeStatement(ts_code="000001.SZ", end_date=date(2025, 12, 31), data={}),
            IncomeStatement(ts_code="600519.SH", end_date=date(2026, 6, 30),
                            ann_date=date(2026, 9, 1), data={}),
        ])
    snapshot = CoreSnapshotRepository(factory).load(AS_OF, ("600519.SH",))
    assert coverage(snapshot).details["missingDisclosureCount"] == 1
    assert snapshot.financial_points == ()
    assert coverage(CoreSnapshotRepository(factory).load(date(2025, 1, 1))).details["missingDisclosureCount"] == 0


@pytest.mark.parametrize("dates", [{"ann_date": date(2026, 3, 1)}, {"f_ann_date": date(2026, 3, 1)}])
def test_known_disclosure_stays_visible_and_is_not_counted_missing(dates):
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="sample", status="L", data={}))
        session.add(IncomeStatement(ts_code="600519.SH", end_date=date(2025, 12, 31), report_type="1",
                                    data={"revenue": 100}, **dates))
    snapshot = CoreSnapshotRepository(factory).load(AS_OF)
    assert coverage(snapshot).passed
    assert coverage(snapshot).details["missingDisclosureCount"] == 0
    assert snapshot.security_inputs["600519.SH"].values["revenue"] == 100


def test_explicit_undisclosed_points_and_filtered_counts_are_combined_once():
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="sample", status="L", data={}))
        session.add(IncomeStatement(ts_code="600519.SH", end_date=date(2025, 12, 31), data={}))
    snapshot = CoreSnapshotRepository(factory).load(AS_OF)
    snapshot = replace(snapshot, financial_points=(FinancialPoint(date(2025, 6, 30), None, "1", {}),))
    assert coverage(snapshot).details["missingDisclosureCount"] == 2


def test_missing_disclosure_is_persisted_as_advisory_and_does_not_change_inputs():
    factory = publication_factory()
    _seed_complete_core_sample(factory)
    before = CoreSnapshotRepository(factory).load(AS_OF, ("600519.SH",)).security_inputs
    with factory.begin() as session:
        session.add(IncomeStatement(ts_code="600519.SH", end_date=date(2025, 9, 30),
                                    data={"revenue": 999999, "n_income": 999999}))
    snapshot = CoreSnapshotRepository(factory).load(AS_OF, ("600519.SH",))
    assert snapshot.security_inputs == before
    report = _real_build_and_publish_job(factory).run(BuildAndPublishOptions(AS_OF, ("600519.SH",)))
    assert report.status == "completed"
    assert {"source": "source-financial-coverage", "check": "source-financial-coverage"} in report.missing_data
    with factory() as session:
        stored = session.scalar(select(QualityCheckResult).where(
            QualityCheckResult.job_run_id == report.job_run_id,
            QualityCheckResult.check_name == "source-financial-coverage"))
        assert stored is not None and not stored.passed and stored.details["severity"] == "advisory"
        assert stored.details["missingDisclosureCount"] == 1
