from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Callable, Literal

from app.connectors.tushare.strategies import Dataset, RequestPlan, TushareMode
from app.stocks.jobs.backfill import PlanFailure
from app.jobs.build_and_publish import (
    BuildAndPublishOptions,
    BuildAndPublishReport,
    sanitize_error_message,
)
from app.stocks.jobs.daily_sync_history import (
    DailySyncExecutionContext,
    DailySyncFinish,
    DailySyncHistory,
)
from app.stocks.jobs.daily_sync_plans import DailySyncPlanBuilder
from app.jobs.failure_retries import serialize_replay_request
from app.jobs.locks import (
    JobLock,
    daily_sync_lock_key,
    data_publication_lock_key,
)
from app.marts.contracts import QualityResult
from app.stocks.universes.models import UniverseSelection


DailySyncStatus = Literal["completed", "partial", "failed", "skipped", "locked"]


@dataclass(frozen=True)
class DailySyncOptions:
    business_date: date
    mode: TushareMode
    symbols: tuple[str, ...] = ()
    lookback_days: int = 7
    force: bool = False
    execution: DailySyncExecutionContext = DailySyncExecutionContext()


@dataclass(frozen=True)
class DailySyncReport:
    status: DailySyncStatus
    job_run_id: str
    business_date: date
    mode: TushareMode
    universe: UniverseSelection
    request_count: int = 0
    provider_count: int = 0
    retained_count: int = 0
    filtered_count: int = 0
    row_count: int = 0
    rejected_count: int = 0
    affected_symbols: tuple[str, ...] = ()
    failed_datasets: tuple[Dataset, ...] = ()
    version: str | None = None
    products: tuple[str, ...] = ()
    quality: tuple[QualityResult, ...] = ()
    error: str | None = None
    publication_release_error: str | None = None
    release_version: str | None = None
    promoted_products: tuple[str, ...] = ()
    current_versions: tuple[tuple[str, str], ...] = ()

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "jobRunId": self.job_run_id,
            "businessDate": self.business_date.isoformat(),
            "mode": self.mode.value,
            "universe": self.universe.to_payload(),
            "requestCount": self.request_count,
            "providerCount": self.provider_count,
            "retainedCount": self.retained_count,
            "filteredCount": self.filtered_count,
            "rowCount": self.row_count,
            "rejectedCount": self.rejected_count,
            "affectedSymbols": list(self.affected_symbols),
            "failedDatasets": [dataset.value for dataset in self.failed_datasets],
            "version": self.version,
            "products": list(self.products),
            "quality": [_quality_payload(result) for result in self.quality],
        }
        if self.error is not None:
            payload["error"] = {
                "errorType": "DailySyncError",
                "errorCode": "daily_sync_failed",
                "message": self.error,
            }
        if self.publication_release_error is not None:
            payload["publicationReleaseError"] = self.publication_release_error
        if self.release_version is not None:
            payload["releaseVersion"] = self.release_version
            payload["promotedProducts"] = list(self.promoted_products)
            payload["currentVersions"] = dict(self.current_versions)
        return payload


@dataclass(frozen=True)
class _StageResult:
    name: str
    request_count: int = 0
    provider_count: int = 0
    retained_count: int = 0
    filtered_count: int = 0
    row_count: int = 0
    rejected_count: int = 0
    failures: tuple[PlanFailure, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": "failed" if self.failures else "completed",
            "requestCount": self.request_count,
            "providerCount": self.provider_count,
            "retainedCount": self.retained_count,
            "filteredCount": self.filtered_count,
            "rowCount": self.row_count,
            "rejectedCount": self.rejected_count,
            "failedDatasets": sorted(
                {failure.dataset.value for failure in self.failures}
            ),
        }


def _quality_payload(result: QualityResult) -> dict[str, object]:
    return {
        "name": result.name,
        "severity": result.severity,
        "passed": result.passed,
        "details": dict(result.details),
    }


class DailySyncJob:
    def __init__(
        self,
        *,
        runner,
        plan_builder_factory: Callable[[date, int], DailySyncPlanBuilder],
        repository,
        build_job,
        history: DailySyncHistory,
        lock: JobLock,
        publication_lock: JobLock,
        universe: UniverseSelection,
        sanitize_error: Callable[[str], str] | None = None,
    ) -> None:
        self._runner = runner
        self._plan_builder_factory = plan_builder_factory
        self._repository = repository
        self._build_job = build_job
        self._history = history
        self._lock = lock
        self._publication_lock = publication_lock
        self._universe = universe
        self._sanitize_error = sanitize_error or sanitize_error_message

    def run(self, options: DailySyncOptions) -> DailySyncReport:
        lock_value = daily_sync_lock_key(options.business_date)
        if not self._lock.try_acquire(lock_value):
            details = {"mode": options.mode.value, "reason": "lock-conflict"}
            run_id = self._history.record_terminal(
                options.business_date,
                options.mode,
                self._universe,
                status="locked",
                details=details,
                execution=options.execution,
            )
            return DailySyncReport(
                "locked",
                run_id,
                options.business_date,
                options.mode,
                self._universe,
            )

        try:
            report = self._run_acquired(options)
        except BaseException:
            try:
                self._lock.release(lock_value)
            except Exception:
                pass
            raise

        try:
            self._lock.release(lock_value)
        except Exception as exc:
            if report.status in ("partial", "failed"):
                return report
            return replace(
                report,
                status="failed",
                error=self._sanitize_error(str(exc)),
            )
        return report

    def _run_acquired(self, options: DailySyncOptions) -> DailySyncReport:
        if (
            self._history.has_completed(options.business_date, self._universe)
            and not options.force
        ):
            details = {"mode": options.mode.value, "reason": "already-completed"}
            run_id = self._history.record_terminal(
                options.business_date,
                options.mode,
                self._universe,
                status="skipped",
                details=details,
                execution=options.execution,
            )
            return DailySyncReport(
                "skipped",
                run_id,
                options.business_date,
                options.mode,
                self._universe,
            )

        run_id = self._history.start(
            options.business_date,
            options.mode,
            self._universe,
            execution=options.execution,
        )
        try:
            return self._run_started(run_id, options)
        except Exception as exc:
            message = self._sanitize_error(str(exc))
            report = DailySyncReport(
                "failed",
                run_id,
                options.business_date,
                options.mode,
                self._universe,
                error=message,
            )
            try:
                self._history.finish(
                    run_id,
                    DailySyncFinish(
                        status="failed",
                        request_count=0,
                        row_count=0,
                        details=self._details(report, (), None),
                        error_message=message,
                    ),
                )
            except Exception:
                pass
            return report

    def _run_started(
        self,
        run_id: str,
        options: DailySyncOptions,
    ) -> DailySyncReport:
        self._validate_symbols(options.symbols)
        builder = self._plan_builder_factory(
            options.business_date,
            options.lookback_days,
        )
        stages: list[_StageResult] = []
        nonblocking_failures: list[PlanFailure] = []

        trading_day = self._repository.trading_day(options.business_date)
        if trading_day is None:
            calendar = self._run_stage(
                run_id,
                "calendar",
                builder.calendar(),
                options.mode,
                stop_on_failure=True,
            )
            stages.append(calendar)
            if calendar.failures:
                return self._finish_failed(run_id, options, stages, calendar.failures)
            trading_day = self._repository.trading_day(options.business_date)
            if trading_day is None:
                plan = builder.calendar()[0]
                failure = self._make_failure(
                    plan,
                    options.mode,
                    "calendar_unknown",
                    "trading calendar remains unknown after refresh",
                    retryable=True,
                )
                self._history.record_failure(run_id, failure)
                unknown = _StageResult("calendar-query", failures=(failure,))
                stages.append(unknown)
                return self._finish_failed(run_id, options, stages, (failure,))

        if trading_day is False:
            report = self._report(
                "skipped",
                run_id,
                options,
                stages,
                (),
                (),
                None,
            )
            self._finish_history(report, stages, None)
            return report

        bootstrap = builder.bootstrap()
        if not self._repository.has_active_securities():
            security = self._run_stage(
                run_id,
                "security",
                tuple(plan for plan in bootstrap if plan.dataset is Dataset.SECURITY),
                options.mode,
                stop_on_failure=True,
            )
            stages.append(security)
            if security.failures:
                return self._finish_failed(run_id, options, stages, security.failures)

        classification = self._run_stage(
            run_id,
            "classification",
            tuple(plan for plan in bootstrap if plan.dataset is Dataset.CLASSIFICATION),
            options.mode,
        )
        stages.append(classification)
        nonblocking_failures.extend(classification.failures)

        market = self._run_stage(
            run_id,
            "critical-market",
            builder.critical_market(options.mode),
            options.mode,
            stop_on_failure=True,
        )
        stages.append(market)
        if market.failures:
            return self._finish_failed(
                run_id,
                options,
                stages,
                (*nonblocking_failures, *market.failures),
            )

        discovery = self._run_stage(
            run_id,
            "discovery",
            builder.discovery(options.mode),
            options.mode,
        )
        stages.append(discovery)
        nonblocking_failures.extend(discovery.failures)

        targets = self._repository.disclosure_targets(
            builder.window_start,
            options.business_date,
            self._universe.symbols,
        )
        corporate_symbols = self._repository.corporate_symbols(
            builder.window_start,
            options.business_date,
            self._universe.symbols,
        )
        affected_symbols = builder.merge_symbols(
            options.symbols,
            (target.symbol for target in targets),
            corporate_symbols,
        )
        periods = builder.merge_periods(target.period for target in targets)

        financial = self._run_stage(
            run_id,
            "financial",
            builder.financial(options.mode, affected_symbols, periods),
            options.mode,
        )
        stages.append(financial)
        nonblocking_failures.extend(financial.failures)

        # 日行情为全池采集，已发布工作台即使没有新公告，报价、截止日及派息状态也会变化。
        # 显式范围依旧只选择该范围和已发现需更新的证券，不扩大财报采集。
        if not options.symbols:
            affected_symbols = builder.merge_symbols(affected_symbols,
                self._repository.published_workbenches(self._universe.symbols, options.business_date))
        build_report, publication_release_error = self._build_with_publication_lock(
            BuildAndPublishOptions(
                as_of=options.business_date,
                symbols=(),
                workbench_symbols=affected_symbols,
            )
        )
        if build_report is None:
            report = self._report(
                "locked",
                run_id,
                options,
                stages,
                affected_symbols,
                tuple(nonblocking_failures),
                None,
                error="data publication lock is already held",
            )
            self._finish_history(report, stages, None)
            return report
        if build_report.status != "completed":
            message = self._sanitize_error(
                str(build_report.error or "build and publish failed")
            )
            report = self._report(
                "failed",
                run_id,
                options,
                stages,
                affected_symbols,
                tuple(nonblocking_failures),
                build_report,
                error=message,
                publication_release_error=publication_release_error,
            )
            self._finish_history(report, stages, build_report)
            return report

        if publication_release_error is not None:
            report = self._report(
                "failed",
                run_id,
                options,
                stages,
                affected_symbols,
                tuple(nonblocking_failures),
                build_report,
                error=publication_release_error,
                publication_release_error=publication_release_error,
            )
            self._finish_history(report, stages, build_report)
            return report

        status: DailySyncStatus = (
            "partial" if nonblocking_failures else "completed"
        )
        error = (
            self._sanitize_error(
                f"{len(nonblocking_failures)} non-blocking request(s) failed"
            )
            if nonblocking_failures
            else None
        )
        report = self._report(
            status,
            run_id,
            options,
            stages,
            affected_symbols,
            tuple(nonblocking_failures),
            build_report,
            error=error,
        )
        self._finish_history(report, stages, build_report)
        return report

    def _build_with_publication_lock(
        self,
        options: BuildAndPublishOptions,
    ) -> tuple[BuildAndPublishReport | None, str | None]:
        lock_value = data_publication_lock_key()
        if not self._publication_lock.try_acquire(lock_value):
            return None, None
        try:
            report = self._build_job.run(options)
        except BaseException:
            try:
                self._publication_lock.release(lock_value)
            except Exception:
                pass
            raise
        release_error: str | None = None
        try:
            self._publication_lock.release(lock_value)
        except Exception as exc:
            release_error = self._sanitize_error(str(exc))
        return report, release_error

    def _run_stage(
        self,
        run_id: str,
        name: str,
        plans: tuple[RequestPlan, ...],
        mode: TushareMode,
        *,
        stop_on_failure: bool = False,
    ) -> _StageResult:
        request_count = 0
        provider_count = 0
        retained_count = 0
        filtered_count = 0
        row_count = 0
        rejected_count = 0
        failures: list[PlanFailure] = []
        for plan in plans:
            failure: PlanFailure | None = None
            try:
                result = self._runner(plan)
                request_count += int(getattr(result, "request_count", 1))
                result_row_count = int(getattr(result, "row_count", 0))
                result_rejected_count = int(getattr(result, "rejected_count", 0))
                result_provider_count = int(
                    getattr(
                        result,
                        "received_count",
                        result_row_count + result_rejected_count,
                    )
                )
                result_retained_count = int(
                    getattr(result, "retained_count", result_provider_count)
                )
                provider_count += result_provider_count
                retained_count += result_retained_count
                filtered_count += int(getattr(result, "filtered_count", 0))
                row_count += int(getattr(result, "row_count", 0))
                rejected_count += result_rejected_count
                if result_rejected_count > 0:
                    failure = self._make_failure(
                        plan,
                        mode,
                        "rejected_rows",
                        "ingestion rejected one or more rows",
                        retryable=False,
                    )
                elif not bool(getattr(result, "completed", True)):
                    failure = self._make_failure(
                        plan,
                        mode,
                        "incomplete_page",
                        "paginated ingestion did not complete",
                        retryable=True,
                    )
            except Exception as exc:
                partial = getattr(exc, "partial_result", None)
                if partial is not None:
                    request_count += int(getattr(partial, "request_count", 0))
                    partial_row_count = int(getattr(partial, "row_count", 0))
                    partial_rejected_count = int(
                        getattr(partial, "rejected_count", 0)
                    )
                    partial_provider_count = int(
                        getattr(
                            partial,
                            "received_count",
                            partial_row_count + partial_rejected_count,
                        )
                    )
                    provider_count += partial_provider_count
                    retained_count += int(
                        getattr(partial, "retained_count", partial_provider_count)
                    )
                    filtered_count += int(
                        getattr(partial, "filtered_count", 0)
                    )
                    row_count += partial_row_count
                    rejected_count += partial_rejected_count
                replay_plan = getattr(exc, "request_plan", None) or plan
                replay_mode = getattr(exc, "mode", None) or mode
                failure = PlanFailure(
                    dataset=plan.dataset,
                    request_key=plan.cursor_key,
                    error_code=str(
                        getattr(exc, "error_code", type(exc).__name__)
                    ),
                    message=self._sanitize_error(str(exc)),
                    retryable=bool(getattr(exc, "retryable", False)),
                    request_payload=serialize_replay_request(
                        replay_plan,
                        replay_mode,
                    ),
                )
            if failure is not None:
                failures.append(failure)
                self._history.record_failure(run_id, failure)
                if stop_on_failure:
                    break
        return _StageResult(
            name=name,
            request_count=request_count,
            provider_count=provider_count,
            retained_count=retained_count,
            filtered_count=filtered_count,
            row_count=row_count,
            rejected_count=rejected_count,
            failures=tuple(failures),
        )

    def _validate_symbols(self, symbols: tuple[str, ...]) -> None:
        outside = sorted(set(symbols) - set(self._universe.symbols))
        if outside:
            raise ValueError(
                f"symbol {outside[0]} is outside universe {self._universe.profile}"
            )

    def _make_failure(
        self,
        plan: RequestPlan,
        mode: TushareMode,
        error_code: str,
        message: str,
        *,
        retryable: bool,
    ) -> PlanFailure:
        return PlanFailure(
            dataset=plan.dataset,
            request_key=plan.cursor_key,
            error_code=error_code,
            message=self._sanitize_error(message),
            retryable=retryable,
            request_payload=serialize_replay_request(plan, mode),
        )

    def _finish_failed(
        self,
        run_id: str,
        options: DailySyncOptions,
        stages: list[_StageResult],
        failures: tuple[PlanFailure, ...],
    ) -> DailySyncReport:
        message = self._sanitize_error(failures[-1].message)
        report = self._report(
            "failed",
            run_id,
            options,
            stages,
            (),
            failures,
            None,
            error=message,
        )
        self._finish_history(report, stages, None)
        return report

    def _report(
        self,
        status: DailySyncStatus,
        run_id: str,
        options: DailySyncOptions,
        stages: list[_StageResult],
        affected_symbols: tuple[str, ...],
        failures: tuple[PlanFailure, ...],
        build_report,
        *,
        error: str | None = None,
        publication_release_error: str | None = None,
    ) -> DailySyncReport:
        return DailySyncReport(
            status=status,
            job_run_id=run_id,
            business_date=options.business_date,
            mode=options.mode,
            universe=self._universe,
            request_count=sum(stage.request_count for stage in stages),
            provider_count=sum(stage.provider_count for stage in stages),
            retained_count=sum(stage.retained_count for stage in stages),
            filtered_count=sum(stage.filtered_count for stage in stages),
            row_count=sum(stage.row_count for stage in stages),
            rejected_count=sum(stage.rejected_count for stage in stages),
            affected_symbols=tuple(sorted(set(affected_symbols))),
            failed_datasets=tuple(
                Dataset(value)
                for value in sorted({failure.dataset.value for failure in failures})
            ),
            version=getattr(build_report, "version", None),
            products=tuple(sorted(set(getattr(build_report, "products", ())))),
            quality=tuple(getattr(build_report, "quality", ())),
            error=error,
            publication_release_error=publication_release_error,
            release_version=getattr(build_report, "release_version", None),
            promoted_products=tuple(
                sorted(getattr(build_report, "promoted_products", ()))
            ),
            current_versions=tuple(
                sorted(getattr(build_report, "current_versions", ()))
            ),
        )

    def _finish_history(
        self,
        report: DailySyncReport,
        stages: list[_StageResult],
        build_report,
    ) -> None:
        self._history.finish(
            report.job_run_id,
            DailySyncFinish(
                status=report.status,
                request_count=report.request_count,
                row_count=report.row_count,
                details=self._details(report, stages, build_report),
                error_message=report.error,
            ),
        )

    @staticmethod
    def _details(
        report: DailySyncReport,
        stages: tuple[_StageResult, ...] | list[_StageResult],
        build_report,
    ) -> dict[str, object]:
        return {
            "mode": report.mode.value,
            "universe": report.universe.to_payload(),
            "stages": [stage.to_payload() for stage in stages],
            "providerCount": report.provider_count,
            "retainedCount": report.retained_count,
            "filteredCount": report.filtered_count,
            "rowCount": report.row_count,
            "rejectedCount": report.rejected_count,
            "affectedSymbols": list(report.affected_symbols),
            "failedDatasets": [
                dataset.value for dataset in report.failed_datasets
            ],
            "buildJobRunId": getattr(build_report, "job_run_id", None),
            "version": report.version,
            "products": list(report.products),
            "quality": [_quality_payload(result) for result in report.quality],
            "publicationReleaseError": report.publication_release_error,
            "releaseVersion": report.release_version,
            "promotedProducts": list(report.promoted_products),
            "currentVersions": dict(report.current_versions),
        }
