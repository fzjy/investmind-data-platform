from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Literal, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.macro.sanitization import sanitize_macro_value
from app.db.models import JobFailure, JobRun, QualityCheckResult
from app.marts.contracts import QualityResult
from app.macro.catalog import catalog_hashes_match
from app.macro.history_catalog import inherited_gap_contracts


MacroSyncStatus = Literal["completed", "partial", "failed", "skipped", "locked"]
_TRUSTED_FAILURE_CODES = frozenset({
    "TIMEOUT",
    "MACRO_HISTORY_GAP",
    "MACRO_NO_SOURCE",
    "MACRO_PUBLICATION_BLOCKED",
    "MACRO_PUBLICATION_FAILED",
    "MACRO_ROWS_REJECTED",
    "MACRO_SERIES_ERROR",
    "MACRO_SOURCE_ERROR",
    "MACRO_SOURCE_FAILED",
    "MACRO_SYNC_FAILED",
    "MACRO_VALUE_EXPIRED",
    "US_RATES_REQUEST_BUDGET_EXHAUSTED",
    "NBS_SYNC_REQUEST_BUDGET_EXHAUSTED",
    "TUSHARE_MACRO_REQUEST_BUDGET_EXHAUSTED",
    "FRED_MACRO_REQUEST_BUDGET_EXHAUSTED",
    "MACRO_CATCHUP_REQUEST_BUDGET_EXHAUSTED",
})


def macro_safe_error_code(code: str) -> str:
    return code if code in _TRUSTED_FAILURE_CODES else "MACRO_SOURCE_ERROR"


class MacroCatalogVersionHashMismatch(ValueError):
    def __init__(self) -> None:
        super().__init__("MACRO_CATALOG_VERSION_HASH_MISMATCH")


def macro_sync_business_key(as_of: datetime, catalog_version: str, timezone: str) -> str:
    if as_of.utcoffset() is None:
        raise ValueError("as_of must have a timezone")
    local_date = as_of.astimezone(ZoneInfo(timezone)).date()
    iso_year, iso_week, _ = local_date.isocalendar()
    return f"{iso_year}-W{iso_week:02d}:{catalog_version}"


class MacroSyncHistory(Protocol):
    def inspect(self, business_key: str, catalog_version: str, catalog_hash: str) -> tuple[bool, int]: ...

    def unresolved_gaps(
        self, catalog_version: str, catalog_hash: str,
    ) -> dict[str, tuple[tuple[date, date], ...]]: ...

    def start(
        self, business_key: str, as_of: datetime, catalog_version: str,
        catalog_hash: str, trigger: str, attempt: int,
    ) -> str: ...

    def finish(self, run_id: str, status: MacroSyncStatus, details: dict[str, object]) -> None: ...

    def record_terminal(
        self, business_key: str, as_of: datetime, catalog_version: str,
        catalog_hash: str, trigger: str, attempt: int,
        status: Literal["skipped", "locked"],
    ) -> str: ...

    def record_failure(
        self, run_id: str, series_id: str, provider: str,
        code: str, message: str, retryable: bool,
    ) -> None: ...

    def record_quality(self, run_id: str, results: Iterable[QualityResult]) -> None: ...

    def resolve_failures(self, series_id: str, provider: str, as_of: datetime, resolved_at: datetime) -> None: ...


class SqlMacroSyncHistory:
    def __init__(self, session_factory: sessionmaker[Session], *, job_name: str = 'sync-macro',
                 resolution_job_names: tuple[str, ...] | None = None) -> None:
        self._session_factory = session_factory
        self._job_name = job_name
        self._resolution_job_names = resolution_job_names or (job_name,)

    def inspect(self, business_key: str, catalog_version: str, catalog_hash: str) -> tuple[bool, int]:
        with self._session_factory() as session:
            runs = session.scalars(
                select(JobRun).where(JobRun.job_name == self._job_name)
            ).all()
        for run in runs:
            if run.details.get("catalogVersion") == catalog_version and not catalog_hashes_match(run.details.get("catalogHash"), catalog_hash):
                raise MacroCatalogVersionHashMismatch()
        current = [run for run in runs if run.business_key == business_key]
        return any(run.status == "completed" for run in current), 1 + max(
            (int(run.details.get("attempt", 0)) for run in current), default=0
        )

    def gap_snapshot(self, catalog_version: str, catalog_hash: str) -> JobRun | None:
        """返回最近显式缺口审计；调用方只能追加新记录，不改写原记录。"""
        with self._session_factory() as session:
            runs = session.scalars(
                select(JobRun).where(JobRun.job_name == self._job_name)
                .order_by(JobRun.finished_at.desc().nulls_last())
            ).all()
        for run in runs:
            details = run.details
            if (
                run.status not in ("completed", "partial", "failed")
                or details.get("catalogVersion") != catalog_version
                or not catalog_hashes_match(details.get("catalogHash"), catalog_hash)
            ):
                continue
            declared = details.get("unresolvedGaps")
            if declared is None:
                continue
            if not isinstance(declared, dict):
                continue
            return run
        return None

    def unresolved_gaps(
        self, catalog_version: str, catalog_hash: str,
    ) -> dict[str, tuple[tuple[date, date], ...]]:
        snapshot = self.gap_snapshot(catalog_version, catalog_hash)
        inherited_ids: frozenset[str] | None = None
        if snapshot is None and self._job_name == 'sync-macro':
            for frozen, inherited_ids in inherited_gap_contracts(catalog_version, catalog_hash):
                snapshot = self.gap_snapshot(frozen.catalog_version, frozen.catalog_hash)
                if snapshot is not None:
                    break
        if snapshot is None:
            return {}
        declared = snapshot.details["unresolvedGaps"]
        gaps: dict[str, tuple[tuple[date, date], ...]] = {}
        for series_id, windows in declared.items():
            if inherited_ids is not None and series_id not in inherited_ids:
                continue
            if not isinstance(series_id, str) or not isinstance(windows, list):
                continue
            parsed: list[tuple[date, date]] = []
            for window in windows:
                if not isinstance(window, dict):
                    continue
                start, end = window.get("start"), window.get("end")
                if not isinstance(start, str) or not isinstance(end, str):
                    continue
                try:
                    first, last = date.fromisoformat(start), date.fromisoformat(end)
                except ValueError:
                    continue
                if first <= last:
                    parsed.append((first, last))
            if parsed:
                gaps[series_id] = tuple(parsed)
        return gaps

    def start(
        self, business_key: str, as_of: datetime, catalog_version: str,
        catalog_hash: str, trigger: str, attempt: int,
    ) -> str:
        run_id = str(uuid4())
        with self._session_factory.begin() as session:
            session.add(JobRun(
                id=run_id, job_name=self._job_name, business_key=business_key,
                status="running", details=_base_details(as_of, catalog_version, catalog_hash, trigger, attempt),
            ))
        return run_id

    def record_collection_scope(self, run_id: str, series_ids: list[str]) -> None:
        """执行前登记精确范围，供当前状态只读接口识别运行中。"""
        with self._session_factory.begin() as session:
            run = session.get(JobRun, run_id)
            if run is None or run.status != 'running':
                raise LookupError('macro running job not found')
            run.details = {**run.details, 'collectionSeriesIds': sorted(set(series_ids))}

    def finish(self, run_id: str, status: MacroSyncStatus, details: dict[str, object]) -> None:
        with self._session_factory.begin() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise LookupError("macro job run not found")
            safe_details = sanitize_macro_value(details)
            if not isinstance(safe_details, dict):
                raise TypeError("macro details must be an object")
            run.details = {**safe_details, **run.details}
            run.status = status
            run.request_count = int(details.get("requestCount", 0))
            run.row_count = int(details.get("rowCount", 0))
            run.finished_at = datetime.now(UTC)

    def record_terminal(
        self, business_key: str, as_of: datetime, catalog_version: str,
        catalog_hash: str, trigger: str, attempt: int,
        status: Literal["skipped", "locked"],
    ) -> str:
        run_id = str(uuid4())
        with self._session_factory.begin() as session:
            session.add(JobRun(
                id=run_id, job_name=self._job_name, business_key=business_key,
                status=status, finished_at=datetime.now(UTC),
                details=_base_details(as_of, catalog_version, catalog_hash, trigger, attempt),
            ))
        return run_id

    def record_failure(
        self, run_id: str, series_id: str, provider: str,
        code: str, message: str, retryable: bool,
    ) -> None:
        del message
        safe_code = macro_safe_error_code(code)
        with self._session_factory.begin() as session:
            session.add(JobFailure(
                job_run_id=run_id, dataset=f"macro:{series_id}", request_key=provider,
                error_code=safe_code,
                message=safe_code, retryable=retryable,
                request_payload={},
            ))

    def record_quality(self, run_id: str, results: Iterable[QualityResult]) -> None:
        with self._session_factory.begin() as session:
            for result in results:
                safe_details = sanitize_macro_value(result.details)
                session.add(QualityCheckResult(
                    job_run_id=run_id, check_name=result.name,
                    passed=result.passed,
                    details=safe_details if isinstance(safe_details, dict) else {},
                ))

    def resolve_failures(self, series_id: str, provider: str, as_of: datetime, resolved_at: datetime) -> None:
        if as_of.utcoffset() is None or resolved_at.utcoffset() is None:
            raise ValueError("recovery timestamps must be timezone-aware")
        with self._session_factory.begin() as session:
            rows = session.execute(select(JobFailure, JobRun).join(
                JobRun, JobRun.id == JobFailure.job_run_id,
            ).where(JobRun.job_name.in_(self._resolution_job_names), JobFailure.dataset == f"macro:{series_id}",
                    JobFailure.request_key == provider, JobFailure.resolved_at.is_(None)))
            for failure, run in rows:
                if run.job_name != self._job_name and not (
                        provider == 'unavailable' and failure.error_code == 'MACRO_NO_SOURCE'):
                    continue  # 当前窗口不能证明其他命名空间的历史缺口或未知故障已恢复。
                if provider == "unavailable" and failure.error_code != "MACRO_NO_SOURCE":
                    continue
                raw_as_of = run.details.get("asOf")
                if not isinstance(raw_as_of, str):
                    continue  # Legacy ambiguous time cannot be silently resolved by a replay.
                try:
                    failed_as_of = datetime.fromisoformat(raw_as_of)
                except ValueError:
                    continue
                if failed_as_of.utcoffset() is not None and failed_as_of <= as_of:
                    failure.resolved_at = resolved_at


def _base_details(
    as_of: datetime, catalog_version: str, catalog_hash: str,
    trigger: str, attempt: int,
) -> dict[str, object]:
    return {
        "asOf": as_of.isoformat(), "catalogVersion": catalog_version,
        "catalogHash": catalog_hash, "trigger": trigger, "attempt": attempt,
    }
