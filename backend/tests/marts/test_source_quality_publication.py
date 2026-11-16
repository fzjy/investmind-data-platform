from datetime import date

from sqlalchemy import select

from app.db.models import QualityCheckResult, TradeCalendar
from app.jobs.build_and_publish import BuildAndPublishOptions
from backend.tests.jobs.test_build_and_publish_integration import (
    _real_build_and_publish_job, _seed_complete_core_sample,
)
from backend.tests.marts.test_row_consistency import stored_publication


def test_known_closed_source_day_blocks_real_job_and_records_quality(pg_factory):
    factory = pg_factory
    _seed_complete_core_sample(factory)
    job = _real_build_and_publish_job(factory)
    options = BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",))
    first = job.run(options)
    assert first.status == "completed"
    before = stored_publication(factory)
    with factory.begin() as session:
        session.add(TradeCalendar(exchange="SSE", cal_date=options.as_of, is_open=False))
    second = job.run(options)
    assert second.status == "failed"
    assert any(item.name == "source-trading-days" and not item.passed for item in second.quality)
    assert stored_publication(factory) == before
    with factory() as session:
        result = session.scalar(select(QualityCheckResult).where(
            QualityCheckResult.job_run_id == second.job_run_id,
            QualityCheckResult.check_name == "source-trading-days"))
        assert result is not None and not result.passed
