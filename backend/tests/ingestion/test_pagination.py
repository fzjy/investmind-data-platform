from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace

import pytest

from app.connectors.tushare.strategies import Dataset, RequestPlan, TushareMode
from app.ingestion.pagination import PaginatedPlanError, PaginatedPlanRunner, plan_fingerprint
from app.ingestion.pipeline import BatchResult


def make_plan(
    *,
    params: dict[str, str] | None = None,
    fields: tuple[str, ...] = ("ts_code", "trade_date", "close"),
    page_size: int = 5000,
) -> RequestPlan:
    return RequestPlan(
        dataset=Dataset.DAILY,
        api_name="daily",
        params=params or {"ts_code": "600519.SH", "trade_date": "20260817"},
        fields=fields,
        cursor_key="daily:600519.SH",
        page_size=page_size,
    )


class MemoryCursorStore:
    def __init__(
        self,
        initial: Mapping[str, Mapping[str, object]] | None = None,
        *,
        events: list[str] | None = None,
    ) -> None:
        self.values = {
            key: dict(value)
            for key, value in (initial or {}).items()
        }
        self.saved: list[tuple[str, dict[str, object]]] = []
        self.deleted: list[str] = []
        self.events = events
        self.save_failure: BaseException | None = None
        self.delete_failure: BaseException | None = None

    def load(self, cursor_key: str) -> dict[str, object] | None:
        value = self.values.get(cursor_key)
        return None if value is None else dict(value)

    def save(self, cursor_key: str, cursor_value: Mapping[str, object]) -> None:
        if self.events is not None:
            self.events.append(f"save:{cursor_value.get('offset')}")
        if self.save_failure is not None:
            raise self.save_failure
        saved_value = dict(cursor_value)
        self.saved.append((cursor_key, saved_value))
        self.values[cursor_key] = saved_value

    def delete(self, cursor_key: str) -> None:
        if self.events is not None:
            self.events.append("delete")
        if self.delete_failure is not None:
            raise self.delete_failure
        self.deleted.append(cursor_key)
        self.values.pop(cursor_key, None)


class PageRunner:
    def __init__(
        self,
        received_counts: tuple[int, ...],
        *,
        retained_counts: tuple[int, ...] | None = None,
        filtered_counts: tuple[int, ...] | None = None,
        rejected_counts: tuple[int, ...] | None = None,
        exception_at_call: int | None = None,
        exception_factory: Callable[[], BaseException] | None = None,
        events: list[str] | None = None,
    ) -> None:
        self.received_counts = received_counts
        self.retained_counts = retained_counts or received_counts
        self.filtered_counts = filtered_counts or tuple(0 for _ in received_counts)
        self.rejected_counts = rejected_counts or tuple(0 for _ in received_counts)
        self.exception_at_call = exception_at_call
        self.exception_factory = exception_factory or (lambda: RuntimeError("page failed"))
        self.events = events
        self.plans: list[RequestPlan] = []

    def __call__(self, plan: RequestPlan) -> BatchResult:
        if self.events is not None:
            self.events.append(f"page:{plan.params['offset']}")
        self.plans.append(plan)
        call_index = len(self.plans) - 1
        if self.exception_at_call == call_index:
            raise self.exception_factory()
        received_count = self.received_counts[call_index]
        rejected_count = self.rejected_counts[call_index]
        return BatchResult(
            batch_id=f"batch-{call_index + 1}",
            dataset=plan.dataset,
            received_count=received_count,
            row_count=received_count - rejected_count,
            rejected_count=rejected_count,
            retained_count=self.retained_counts[call_index],
            filtered_count=self.filtered_counts[call_index],
        )


def cursor_shape(
    plan: RequestPlan,
    mode: TushareMode,
    page_size: int,
    offset: int,
    fingerprint_context: str | None = None,
) -> dict[str, object]:
    return {
        "offset": offset,
        "pageSize": page_size,
        "planFingerprint": plan_fingerprint(
            plan,
            mode,
            page_size,
            fingerprint_context,
        ),
        "apiName": plan.api_name,
        "dataset": plan.dataset.value,
        "mode": mode.value,
    }


def runner(
    page_runner: PageRunner,
    cursor_store: MemoryCursorStore,
    *,
    mode: TushareMode = TushareMode.STANDARD,
    page_size: int | None = 2,
    fingerprint_context: str | None = None,
) -> PaginatedPlanRunner:
    return PaginatedPlanRunner(
        page_runner,
        cursor_store,
        mode=mode,
        page_size=page_size,
        fingerprint_context=fingerprint_context,
    )


def test_plan_fingerprint_uses_canonical_request_shape() -> None:
    plan = make_plan(
        params={
            "ts_code": "600519.SH",
            "trade_date": "20260817",
            "limit": "999",
            "offset": "123",
        }
    )
    same_plan = replace(plan, params={**plan.params, "limit": "1", "offset": "0"})

    digest = plan_fingerprint(plan, TushareMode.STANDARD, 2)

    assert digest == "f2b3b9cb528aad692082dec673b0eed7391519066bf0d04666a0c1c24febae54"
    assert digest == plan_fingerprint(same_plan, TushareMode.STANDARD, 2)
    assert len(digest) == 64
    assert digest == digest.lower()


def test_plan_fingerprint_changes_when_resumable_inputs_change() -> None:
    plan = make_plan()
    digest = plan_fingerprint(plan, TushareMode.STANDARD, 2)

    variants = (
        (plan, TushareMode.VIP, 2),
        (replace(plan, params={**plan.params, "trade_date": "20260818"}), TushareMode.STANDARD, 2),
        (replace(plan, fields=("trade_date", "ts_code", "close")), TushareMode.STANDARD, 2),
        (plan, TushareMode.STANDARD, 3),
    )

    for variant_plan, mode, page_size in variants:
        assert plan_fingerprint(variant_plan, mode, page_size) != digest


def test_pagination_requests_until_short_page_then_deletes_cursor() -> None:
    page_runner = PageRunner((2, 2, 1))
    cursor_store = MemoryCursorStore()
    plan = make_plan()

    result = runner(page_runner, cursor_store)(plan)

    assert [page.params["offset"] for page in page_runner.plans] == ["0", "2", "4"]
    assert [page.params["limit"] for page in page_runner.plans] == ["2", "2", "2"]
    assert [page.params["ts_code"] for page in page_runner.plans] == ["600519.SH"] * 3
    assert result.dataset is Dataset.DAILY
    assert result.request_key == "daily:600519.SH"
    assert result.pages == 3
    assert result.request_count == 3
    assert result.resumed_from_offset == 0
    assert result.completed is True
    assert tuple(batch.batch_id for batch in result.batches) == ("batch-1", "batch-2", "batch-3")
    assert result.row_count == 5


def test_filtered_short_page_still_uses_provider_count_for_pagination() -> None:
    page_runner = PageRunner(
        (2, 1),
        retained_counts=(1, 1),
        filtered_counts=(1, 0),
    )
    cursor_store = MemoryCursorStore()

    result = runner(page_runner, cursor_store)(make_plan())

    assert [page.params["offset"] for page in page_runner.plans] == ["0", "2"]
    assert result.request_count == 2
    assert result.received_count == 3
    assert result.retained_count == 2
    assert result.filtered_count == 1


def test_universe_fingerprint_change_restarts_saved_cursor_at_zero() -> None:
    plan = make_plan()
    cursor_store = MemoryCursorStore(
        {
            plan.cursor_key: cursor_shape(
                plan,
                TushareMode.STANDARD,
                2,
                8,
                "universe-a",
            )
        }
    )
    page_runner = PageRunner((1,))

    result = runner(
        page_runner,
        cursor_store,
        fingerprint_context="universe-b",
    )(plan)

    assert result.resumed_from_offset == 0
    assert page_runner.plans[0].params["offset"] == "0"
    assert cursor_store.saved[0][1]["offset"] == 0
    assert result.rejected_count == 0
    assert cursor_store.deleted == ["daily:600519.SH"]
    assert cursor_store.load("daily:600519.SH") is None


def test_empty_first_page_returns_one_batch_and_deletes_cursor() -> None:
    page_runner = PageRunner((0,))
    cursor_store = MemoryCursorStore()
    plan = make_plan()

    result = runner(page_runner, cursor_store)(plan)

    assert [page.params["offset"] for page in page_runner.plans] == ["0"]
    assert result.pages == 1
    assert result.request_count == 1
    assert result.row_count == 0
    assert result.rejected_count == 0
    assert result.completed is True
    assert tuple(batch.batch_id for batch in result.batches) == ("batch-1",)
    assert cursor_store.deleted == ["daily:600519.SH"]


def test_matching_saved_cursor_resumes_from_offset() -> None:
    plan = make_plan()
    cursor_store = MemoryCursorStore(
        {plan.cursor_key: cursor_shape(plan, TushareMode.STANDARD, 2, 2)}
    )
    page_runner = PageRunner((1,))

    result = runner(page_runner, cursor_store)(plan)

    assert [page.params["offset"] for page in page_runner.plans] == ["2"]
    assert result.resumed_from_offset == 2
    assert result.pages == 1
    assert result.completed is True


def test_fingerprint_mismatch_replaces_stale_cursor_before_first_request() -> None:
    events: list[str] = []
    plan = make_plan()
    cursor_store = MemoryCursorStore(
        {
            plan.cursor_key: {
                **cursor_shape(plan, TushareMode.STANDARD, 2, 8),
                "planFingerprint": "stale",
            }
        },
        events=events,
    )
    page_runner = PageRunner((1,), events=events)

    result = runner(page_runner, cursor_store)(plan)

    assert [page.params["offset"] for page in page_runner.plans] == ["0"]
    assert cursor_store.saved[0] == (plan.cursor_key, cursor_shape(plan, TushareMode.STANDARD, 2, 0))
    assert events[:2] == ["save:0", "page:0"]
    assert result.resumed_from_offset == 0


def test_rejected_page_returns_incomplete_without_advancing_cursor() -> None:
    plan = make_plan()
    cursor_store = MemoryCursorStore(
        {plan.cursor_key: cursor_shape(plan, TushareMode.STANDARD, 2, 2)}
    )
    original_cursor = cursor_store.load(plan.cursor_key)
    page_runner = PageRunner((2,), rejected_counts=(1,))

    result = runner(page_runner, cursor_store)(plan)

    assert [page.params["offset"] for page in page_runner.plans] == ["2"]
    assert result.completed is False
    assert result.pages == 1
    assert tuple(batch.batch_id for batch in result.batches) == ("batch-1",)
    assert result.row_count == 1
    assert result.rejected_count == 1
    assert cursor_store.load(plan.cursor_key) == original_cursor
    assert cursor_store.saved == []
    assert cursor_store.deleted == []


class RetryablePageError(RuntimeError):
    retryable = True


def test_page_exception_raises_paginated_error_with_prior_batches_and_current_cursor() -> None:
    plan = make_plan()
    cursor_store = MemoryCursorStore()
    page_runner = PageRunner(
        (2,),
        exception_at_call=1,
        exception_factory=lambda: RetryablePageError("temporary outage"),
    )

    with pytest.raises(PaginatedPlanError) as error:
        runner(page_runner, cursor_store)(plan)

    assert str(error.value) == "temporary outage"
    assert error.value.error_code == "RetryablePageError"
    assert error.value.retryable is True
    assert error.value.partial_result.completed is False
    assert error.value.partial_result.pages == 1
    assert error.value.partial_result.request_count == 2
    assert tuple(batch.batch_id for batch in error.value.partial_result.batches) == ("batch-1",)
    assert cursor_store.load(plan.cursor_key) == cursor_shape(plan, TushareMode.STANDARD, 2, 2)


def test_cursor_save_exception_raises_paginated_error_without_later_request() -> None:
    plan = make_plan()
    cursor_store = MemoryCursorStore()
    cursor_store.save_failure = RuntimeError("cannot save cursor")
    page_runner = PageRunner((2, 1))

    with pytest.raises(PaginatedPlanError) as error:
        runner(page_runner, cursor_store)(plan)

    assert str(error.value) == "cannot save cursor"
    assert error.value.error_code == "RuntimeError"
    assert error.value.retryable is False
    assert tuple(batch.batch_id for batch in error.value.partial_result.batches) == ("batch-1",)
    assert [page.params["offset"] for page in page_runner.plans] == ["0"]


def test_stale_cursor_save_exception_raises_paginated_error_before_requesting_page() -> None:
    plan = make_plan()
    cursor_store = MemoryCursorStore(
        {plan.cursor_key: {**cursor_shape(plan, TushareMode.STANDARD, 2, 6), "mode": "vip"}}
    )
    cursor_store.save_failure = RuntimeError("cannot replace cursor")
    page_runner = PageRunner((1,))

    with pytest.raises(PaginatedPlanError) as error:
        runner(page_runner, cursor_store)(plan)

    assert str(error.value) == "cannot replace cursor"
    assert error.value.partial_result.batches == ()
    assert page_runner.plans == []


def test_cursor_delete_exception_raises_paginated_error_without_later_request() -> None:
    plan = make_plan()
    cursor_store = MemoryCursorStore()
    cursor_store.delete_failure = RuntimeError("cannot delete cursor")
    page_runner = PageRunner((1,))

    with pytest.raises(PaginatedPlanError) as error:
        runner(page_runner, cursor_store)(plan)

    assert str(error.value) == "cannot delete cursor"
    assert error.value.error_code == "RuntimeError"
    assert error.value.partial_result.completed is False
    assert tuple(batch.batch_id for batch in error.value.partial_result.batches) == ("batch-1",)
    assert [page.params["offset"] for page in page_runner.plans] == ["0"]


def test_exactly_full_data_requests_extra_empty_page_before_deleting_cursor() -> None:
    page_runner = PageRunner((2, 2, 0))
    cursor_store = MemoryCursorStore()
    plan = make_plan()

    result = runner(page_runner, cursor_store)(plan)

    assert [page.params["offset"] for page in page_runner.plans] == ["0", "2", "4"]
    assert result.completed is True
    assert result.pages == 3
    assert result.request_count == 3
    assert result.row_count == 4
    assert cursor_store.deleted == ["daily:600519.SH"]


def test_second_run_after_completion_starts_again_at_zero() -> None:
    page_runner = PageRunner((1, 1))
    cursor_store = MemoryCursorStore()
    plan = make_plan()
    paginated_runner = runner(page_runner, cursor_store)

    first = paginated_runner(plan)
    second = paginated_runner(plan)

    assert first.completed is True
    assert second.completed is True
    assert [page.params["offset"] for page in page_runner.plans] == ["0", "0"]


def test_rejects_non_positive_effective_page_size() -> None:
    plan = make_plan(page_size=0)
    cursor_store = MemoryCursorStore()
    page_runner = PageRunner((1,))

    with pytest.raises(ValueError, match="page size must be positive"):
        runner(page_runner, cursor_store, page_size=None)(plan)

    assert page_runner.plans == []
    assert cursor_store.saved == []
    assert cursor_store.deleted == []
