from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import hashlib
import json

from app.connectors.tushare.strategies import Dataset, RequestPlan, TushareMode
from app.ingestion.pipeline import BatchResult
from app.jobs.cursors import CursorStore


@dataclass(frozen=True)
class PaginatedResult:
    dataset: Dataset
    request_key: str
    pages: int
    request_count: int
    resumed_from_offset: int
    batches: tuple[BatchResult, ...]
    completed: bool

    @property
    def row_count(self) -> int:
        return sum(batch.row_count for batch in self.batches)

    @property
    def rejected_count(self) -> int:
        return sum(batch.rejected_count for batch in self.batches)

    @property
    def received_count(self) -> int:
        return sum(batch.received_count for batch in self.batches)

    @property
    def retained_count(self) -> int:
        return sum(batch.retained_count for batch in self.batches)

    @property
    def filtered_count(self) -> int:
        return sum(batch.filtered_count for batch in self.batches)


class PaginatedPlanError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        partial_result: PaginatedResult,
        error_code: str,
        retryable: bool,
        request_plan: RequestPlan | None = None,
        mode: TushareMode | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_result = partial_result
        self.error_code = error_code
        self.retryable = retryable
        self.request_plan = request_plan
        self.mode = mode


def plan_fingerprint(
    plan: RequestPlan,
    mode: TushareMode,
    page_size: int,
    fingerprint_context: str | None = None,
) -> str:
    params = {
        key: value
        for key, value in plan.params.items()
        if key not in {"limit", "offset"}
    }
    payload = {
        "dataset": plan.dataset.value,
        "apiName": plan.api_name,
        "mode": mode.value,
        "params": params,
        "fields": list(plan.fields),
        "pageSize": page_size,
    }
    if fingerprint_context is not None:
        payload["fingerprintContext"] = fingerprint_context
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PaginatedPlanRunner:
    def __init__(
        self,
        run_page: Callable[[RequestPlan], BatchResult],
        cursor_store: CursorStore,
        *,
        mode: TushareMode,
        page_size: int | None = None,
        fingerprint_context: str | None = None,
        status_factory=None,
        scope_kind: str = 'historical',
    ) -> None:
        self._status_factory = status_factory
        self._scope_kind = scope_kind
        self._run_page = run_page
        self._cursor_store = cursor_store
        self._mode = mode
        self._page_size = page_size
        self._fingerprint_context = fingerprint_context

    def __call__(self, plan: RequestPlan) -> PaginatedResult:
        if self._status_factory is None:
            return self._run_pages(plan)
        from app.acquisition.adapters.pagination import tracked_pages
        return tracked_pages(self._status_factory,plan,self._scope_kind,self._effective_page_size(plan),
                             plan_fingerprint(plan,self._mode,self._effective_page_size(plan),self._fingerprint_context),
                             self._run_pages)

    def _run_pages(self, plan: RequestPlan) -> PaginatedResult:
        page_size = self._effective_page_size(plan)
        fingerprint = plan_fingerprint(
            plan,
            self._mode,
            page_size,
            self._fingerprint_context,
        )
        cursor = self._cursor_store.load(plan.cursor_key)
        cursor_shape = self._cursor_shape(plan, fingerprint, page_size, 0)
        resumed_from_offset = self._resume_offset(cursor, cursor_shape)
        batches: list[BatchResult] = []
        request_count = 0

        if cursor is not None and resumed_from_offset == 0 and cursor != cursor_shape:
            self._save_cursor(
                plan.cursor_key,
                cursor_shape,
                plan,
                resumed_from_offset,
                batches,
                request_count,
            )

        offset = resumed_from_offset
        while True:
            batch, request_count = self._request_page(
                plan,
                page_size,
                offset,
                resumed_from_offset,
                batches,
                request_count,
            )
            batches.append(batch)

            if batch.rejected_count > 0 or batch.has_more and not batch.received_count:
                return self._result(
                    plan,
                    resumed_from_offset,
                    batches,
                    request_count,
                    completed=False,
                )

            if batch.has_more is False or batch.has_more is None and batch.received_count < page_size:
                self._delete_cursor(
                    plan.cursor_key,
                    plan,
                    resumed_from_offset,
                    batches,
                    request_count,
                )
                return self._result(
                    plan,
                    resumed_from_offset,
                    batches,
                    request_count,
                    completed=True,
                )

            offset += batch.received_count
            self._save_cursor(
                plan.cursor_key,
                self._cursor_shape(plan, fingerprint, page_size, offset),
                plan,
                resumed_from_offset,
                batches,
                request_count,
            )

    def _effective_page_size(self, plan: RequestPlan) -> int:
        page_size = self._page_size if self._page_size is not None else plan.page_size
        if page_size <= 0:
            raise ValueError("page size must be positive")
        return page_size

    def _resume_offset(
        self,
        cursor: Mapping[str, object] | None,
        expected_shape: Mapping[str, object],
    ) -> int:
        if cursor is None:
            return 0
        offset = cursor.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return 0
        for key in ("pageSize", "planFingerprint", "apiName", "dataset", "mode"):
            if cursor.get(key) != expected_shape[key]:
                return 0
        return offset

    def _request_page(
        self,
        plan: RequestPlan,
        page_size: int,
        offset: int,
        resumed_from_offset: int,
        batches: list[BatchResult],
        request_count: int,
    ) -> tuple[BatchResult, int]:
        params = dict(plan.params)
        params["limit"] = str(page_size)
        params["offset"] = str(offset)
        page_plan = replace(plan, params=params)
        try:
            request_count += 1
            return self._run_page(page_plan), request_count
        except Exception as cause:
            raise self._error(cause, plan, resumed_from_offset, batches, request_count) from cause

    def _save_cursor(
        self,
        cursor_key: str,
        cursor_value: Mapping[str, object],
        plan: RequestPlan,
        resumed_from_offset: int,
        batches: list[BatchResult],
        request_count: int,
    ) -> None:
        try:
            self._cursor_store.save(cursor_key, cursor_value)
        except Exception as cause:
            raise self._error(cause, plan, resumed_from_offset, batches, request_count) from cause

    def _delete_cursor(
        self,
        cursor_key: str,
        plan: RequestPlan,
        resumed_from_offset: int,
        batches: list[BatchResult],
        request_count: int,
    ) -> None:
        try:
            self._cursor_store.delete(cursor_key)
        except Exception as cause:
            raise self._error(cause, plan, resumed_from_offset, batches, request_count) from cause

    def _error(
        self,
        cause: Exception,
        plan: RequestPlan,
        resumed_from_offset: int,
        batches: list[BatchResult],
        request_count: int,
    ) -> PaginatedPlanError:
        return PaginatedPlanError(
            str(cause),
            partial_result=self._result(
                plan,
                resumed_from_offset,
                batches,
                request_count,
                completed=False,
            ),
            error_code=type(cause).__name__,
            retryable=bool(getattr(cause, "retryable", False)),
            request_plan=replace(plan, page_size=self._effective_page_size(plan)),
            mode=self._mode,
        )

    def _result(
        self,
        plan: RequestPlan,
        resumed_from_offset: int,
        batches: list[BatchResult],
        request_count: int,
        *,
        completed: bool,
    ) -> PaginatedResult:
        return PaginatedResult(
            dataset=plan.dataset,
            request_key=plan.cursor_key,
            pages=len(batches),
            request_count=request_count,
            resumed_from_offset=resumed_from_offset,
            batches=tuple(batches),
            completed=completed,
        )

    def _cursor_shape(
        self,
        plan: RequestPlan,
        fingerprint: str,
        page_size: int,
        offset: int,
    ) -> dict[str, object]:
        return {
            "offset": offset,
            "pageSize": page_size,
            "planFingerprint": fingerprint,
            "apiName": plan.api_name,
            "dataset": plan.dataset.value,
            "mode": self._mode.value,
        }
