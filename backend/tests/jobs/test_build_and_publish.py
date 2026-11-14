from dataclasses import dataclass
from datetime import date, datetime, timezone
from types import SimpleNamespace
from time import perf_counter

from app.stocks.derived.pipeline import DerivedReport
from app.marts.contracts import MartCandidate, QualityResult, release_version
from app.marts.publication import PublicationOutcome, QualityCheckFailed
from app.jobs.build_and_publish import (
    BuildAndPublishJob,
    BuildAndPublishOptions,
    _duplicate_source_keys,
)


def _candidate(product: str) -> MartCandidate:
    payload = {"product": product}
    return MartCandidate(product, datetime(2026, 8, 24, tzinfo=timezone.utc), payload)


@dataclass
class FakeSnapshot:
    as_of: date = date(2026, 8, 24)
    securities: tuple[object, ...] = (object(), object())
    security_inputs: dict[str, object] = None
    missing_data: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        if self.security_inputs is None:
            self.security_inputs = {"600519.SH": object()}


class FakeRepository:
    def load(self, as_of: date, symbols: tuple[str, ...]):
        return FakeSnapshot(as_of=as_of)


class FailingRepository:
    def load(self, as_of: date, symbols: tuple[str, ...]):
        raise RuntimeError(
            "connection failed: postgresql+psycopg://user:db-password@localhost/db"
            "?token=query-token DANG_API_SECRET=env-secret"
        )


class FakeDerived:
    def run(self, snapshot, symbols):
        return DerivedReport(tuple(symbols), 14, 7, 30, 30)


class FakeBuilders:
    def build(self, snapshot, symbols, version):
        return tuple(
            _candidate(product)
            for product in (
                "market-overview",
                "market-explorer",
                "screener",
                "portfolio",
                "stock-workbench:600519.SH",
            )
        )


class FakeHistory:
    def __init__(self) -> None:
        self.finished: list[tuple[str, str, str | None]] = []

    def start(self, options) -> str:
        return "job-1"

    def record_quality(self, run_id, results) -> None:
        return None

    def finish(self, run_id, status, message=None) -> None:
        self.finished.append((run_id, status, message))


class FakeQualityGate:
    def __init__(self) -> None:
        self.results = (QualityResult("all", "blocking", True, {}),)

    def evaluate(self, candidates, context):
        return self.results


class FakePublisher:
    def __init__(self) -> None:
        self.candidates = ()
        self.version = None
        self.forced_current_version: str | None = None
        self.current_versions: dict[str, str] = {}

    def publish_many(self, candidates, version, results):
        self.candidates = tuple(candidates)
        self.version = version
        if any(not result.passed and result.severity == "blocking" for result in results):
            raise QualityCheckFailed([result.name for result in results if not result.passed])
        current_version = self.forced_current_version or version
        promoted = tuple(
            candidate.product
            for candidate in self.candidates
            if current_version == version
            and self.current_versions.get(candidate.product) != version
        )
        self.current_versions.update(
            {candidate.product: current_version for candidate in self.candidates}
        )
        return PublicationOutcome(tuple(
            SimpleNamespace(product=candidate.product, version=current_version)
            for candidate in self.candidates
        ), promoted)


def _dependencies():
    history = FakeHistory()
    gate = FakeQualityGate()
    publisher = FakePublisher()
    job = BuildAndPublishJob(
        repository=FakeRepository(),
        derived=FakeDerived(),
        builders=FakeBuilders(),
        quality=gate,
        publisher=publisher,
        history=history,
    )
    return job, gate, history, publisher


def test_job_builds_checks_and_publishes_one_release() -> None:
    job, _gate, _history, _publisher = _dependencies()

    report = job.run(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))

    assert report.status == "completed"
    assert report.version.startswith("20260824-")
    assert set(report.products) >= {
        "market-overview",
        "market-explorer",
        "screener",
        "portfolio",
        "stock-workbench:600519.SH",
    }


def test_stale_release_report_distinguishes_built_release_from_current_pointers() -> None:
    job, _gate, _history, publisher = _dependencies()
    publisher.forced_current_version = "20260914-current"

    report = job.run(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))

    assert report.status == "completed"
    assert report.release_version is not None
    assert report.release_version.startswith("20260824-")
    assert report.version == "20260914-current"
    assert report.promoted_products == ()
    assert dict(report.current_versions) == {
        product: "20260914-current" for product in report.products
    }


def test_history_completion_failure_retains_committed_publication_summary() -> None:
    job, _gate, history, _publisher = _dependencies()
    original_finish = history.finish

    def fail_completed_finish(run_id, status, message=None) -> None:
        if status == "completed":
            raise RuntimeError("history finish token=secret-token")
        original_finish(run_id, status, message)

    history.finish = fail_completed_finish

    report = job.run(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))

    assert report.status == "failed"
    assert report.error == "history finish token=***"
    assert report.release_version is not None
    assert report.version == report.release_version
    assert set(report.promoted_products) == set(report.products)
    assert dict(report.current_versions) == {
        product: report.release_version for product in report.products
    }
    assert history.finished[-1] == ("job-1", "failed", report.error)


def test_empty_history_completion_error_keeps_failed_status_and_publication_truth() -> None:
    job, _gate, history, _publisher = _dependencies()
    original_finish = history.finish

    def fail_completed_finish(run_id, status, message=None) -> None:
        if status == "completed":
            raise Exception()
        original_finish(run_id, status, message)

    history.finish = fail_completed_finish

    report = job.run(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))

    assert report.status == "failed"
    assert report.error == "Exception"
    assert report.release_version == report.version
    assert report.release_version is not None
    assert set(report.promoted_products) == set(report.products)
    assert dict(report.current_versions) == {
        product: report.release_version for product in report.products
    }
    assert history.finished[-1] == ("job-1", "failed", report.error)


def test_empty_repository_error_does_not_mark_unpublished_run_completed() -> None:
    job, _gate, history, _publisher = _dependencies()

    def fail_load(_as_of, _symbols):
        raise Exception()

    job._repository.load = fail_load

    report = job.run(BuildAndPublishOptions(date(2026, 8, 24)))

    assert report.status == "failed"
    assert report.error == "Exception"
    assert report.release_version is None
    assert report.current_versions == ()
    assert history.finished[-1] == ("job-1", "failed", report.error)


def test_history_failure_on_both_finish_attempts_still_returns_publication_truth() -> None:
    job, _gate, history, _publisher = _dependencies()

    def fail_finish(_run_id, _status, _message=None) -> None:
        raise RuntimeError("history unavailable token=secret-token")

    history.finish = fail_finish

    report = job.run(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))

    assert report.status == "failed"
    assert report.error == "history unavailable token=***"
    assert report.release_version == report.version
    assert set(report.promoted_products) == set(report.products)
    assert dict(report.current_versions) == {
        product: report.release_version for product in report.products
    }


def test_same_release_rerun_reports_no_new_promotion() -> None:
    job, _gate, _history, _publisher = _dependencies()
    options = BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",))

    first = job.run(options)
    replay = job.run(options)

    assert set(first.promoted_products) == set(first.products)
    assert replay.status == "completed"
    assert replay.version == first.version
    assert replay.release_version == first.release_version
    assert replay.promoted_products == ()
    assert dict(replay.current_versions) == dict(first.current_versions)


def test_blocking_quality_failure_preserves_previous_release() -> None:
    job, gate, history, _publisher = _dependencies()
    gate.results = (QualityResult("ohlc", "blocking", False, {}),)

    report = job.run(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))

    assert report.status == "failed"
    assert history.finished[-1][1] == "failed"


def test_advisory_missing_data_is_embedded_before_versioning() -> None:
    job, gate, _history, publisher = _dependencies()
    gate.results = (
        QualityResult(
            "external-data-coverage",
            "advisory",
            False,
            {"missingSources": ["macro_observation"]},
        ),
    )

    report = job.run(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))

    assert report.status == "completed"
    for candidate in publisher.candidates:
        assert {item["source"] for item in candidate.payload["missingData"]} == {
            "macro_observation"
        }
        assert candidate.missing_data == (
            {"source": "macro_observation", "check": "external-data-coverage"},
        )
    assert report.missing_data == (
        {"source": "macro_observation", "check": "external-data-coverage"},
    )
    assert publisher.version == release_version(date(2026, 8, 24), publisher.candidates)


def test_job_redacts_common_secrets_without_injected_sanitizer() -> None:
    history = FakeHistory()
    job = BuildAndPublishJob(
        repository=FailingRepository(),
        derived=FakeDerived(),
        builders=FakeBuilders(),
        quality=FakeQualityGate(),
        publisher=FakePublisher(),
        history=history,
    )

    report = job.run(BuildAndPublishOptions(date(2026, 8, 24)))

    assert report.status == "failed"
    assert report.error is not None
    assert "db-password" not in report.error
    assert "query-token" not in report.error
    assert "env-secret" not in report.error
    assert report.error.count("***") == 3
    assert history.finished[-1][2] == report.error


def test_quality_context_reports_duplicate_source_logical_keys() -> None:
    duplicate_price = SimpleNamespace(
        ts_code="600519.SH",
        trade_date=date(2026, 8, 24),
    )
    snapshot = FakeSnapshot()
    snapshot.daily_prices = (duplicate_price, duplicate_price)

    context = BuildAndPublishJob._quality_context(
        snapshot,
        (),
        DerivedReport((), 0, 0, 0, 0),
    )

    assert context.source_duplicate_keys == (
        "core.daily_price:600519.SH:2026-08-24",
    )


def test_source_duplicate_detection_scales_linearly() -> None:
    rows = tuple(
        SimpleNamespace(
            ts_code=f"{index:06d}.SH",
            trade_date=date(2026, 8, 24),
        )
        for index in range(20_000)
    )
    snapshot = SimpleNamespace(daily_prices=rows)

    started = perf_counter()
    duplicates = _duplicate_source_keys(snapshot)
    elapsed = perf_counter() - started

    assert duplicates == ()
    assert elapsed < 3
