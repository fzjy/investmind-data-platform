from __future__ import annotations

from datetime import date
from dataclasses import dataclass
from typing import Any

from app.connectors.tushare.strategies import Dataset, RequestPlan, SyncScope, TushareMode, build_request_plans
from app.jobs.locks import JobLock, ProcessLocalJobLock


def is_trading_day(value: date, open_days: set[date]) -> bool:
    return value in open_days


@dataclass(frozen=True)
class IncrementalReport:
    business_date: date
    skipped: bool
    completed: int
    failed: int
    results: tuple[Any, ...]


class IncrementalJob:
    def __init__(self, runner, *, lock: JobLock | None = None) -> None:
        self._runner = runner
        self._lock = lock or ProcessLocalJobLock()

    def run(self, business_date: date, open_days: set[date], mode: TushareMode, datasets: tuple[Dataset, ...], symbols: tuple[str, ...] = ()) -> IncrementalReport:
        if not is_trading_day(business_date, open_days):
            return IncrementalReport(business_date, True, 0, 0, ())
        key = f"incremental:{business_date.isoformat()}"
        if not self._lock.try_acquire(key):
            return IncrementalReport(business_date, False, 0, len(datasets), ())
        plans = tuple(plan for dataset in datasets for plan in build_request_plans(dataset, mode, SyncScope(symbols=symbols, trade_dates=(business_date,))))
        results: list[Any] = []
        failed = 0
        try:
            for plan in plans:
                try:
                    results.append(self._runner(plan))
                except Exception:
                    failed += 1
        finally:
            self._lock.release(key)
        return IncrementalReport(business_date, False, len(results), failed, tuple(results))
