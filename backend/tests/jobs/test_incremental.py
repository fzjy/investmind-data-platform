from __future__ import annotations

from datetime import date

from app.stocks.jobs.incremental import IncrementalJob, is_trading_day
from app.connectors.tushare.strategies import Dataset, TushareMode


def test_weekend_is_not_a_trading_day() -> None:
    assert is_trading_day(date(2026, 8, 15), {date(2026, 8, 14)}) is False


def test_known_open_day_is_trading_day() -> None:
    assert is_trading_day(date(2026, 8, 14), {date(2026, 8, 14)}) is True


def test_incremental_skips_non_trading_day() -> None:
    job = IncrementalJob(lambda plan: plan)
    report = job.run(date(2026, 8, 15), {date(2026, 8, 14)}, TushareMode.STANDARD, (Dataset.DAILY,))
    assert report.skipped is True


def test_legacy_incremental_job_still_runs_open_day_plans() -> None:
    calls = []
    job = IncrementalJob(lambda plan: calls.append(plan) or plan)

    report = job.run(
        date(2026, 8, 14),
        {date(2026, 8, 14)},
        TushareMode.STANDARD,
        (Dataset.DAILY,),
    )

    assert report.skipped is False
    assert report.completed == 1
    assert report.failed == 0
    assert [plan.dataset for plan in calls] == [Dataset.DAILY]
