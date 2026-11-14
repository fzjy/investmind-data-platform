from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from app.connectors.tushare.strategies import (
    Dataset,
    RequestPlan,
    SyncScope,
    TushareMode,
    build_request_plans,
)


CRITICAL_MARKET_DATASETS = (Dataset.DAILY, Dataset.DAILY_BASIC, Dataset.ADJ_FACTOR)
CORPORATE_WINDOW_DATASETS = (
    Dataset.REPURCHASE,
    Dataset.SHARE_UNLOCK,
    Dataset.SHAREHOLDER_COUNT,
)
FINANCIAL_DATASETS = (
    Dataset.INCOME,
    Dataset.BALANCE_SHEET,
    Dataset.CASHFLOW,
    Dataset.FINANCIAL_INDICATOR,
    Dataset.FINANCIAL_FORECAST,
    Dataset.DIVIDEND,
    Dataset.BUSINESS_SEGMENT,
    Dataset.TOP_HOLDER,
)


@dataclass(frozen=True)
class DailySyncPlanBuilder:
    business_date: date
    lookback_days: int = 7

    def __post_init__(self) -> None:
        if not 1 <= self.lookback_days <= 31:
            raise ValueError("lookback_days must be between 1 and 31")

    @property
    def window_start(self) -> date:
        return self.business_date - timedelta(days=self.lookback_days - 1)

    def calendar(self) -> tuple[RequestPlan, ...]:
        return tuple(
            build_request_plans(
                Dataset.TRADE_CALENDAR,
                TushareMode.STANDARD,
                SyncScope(start_date=self.business_date, end_date=self.business_date),
            )
        )

    def bootstrap(self) -> tuple[RequestPlan, ...]:
        return tuple(
            plan
            for dataset in (Dataset.SECURITY, Dataset.CLASSIFICATION)
            for plan in build_request_plans(dataset, TushareMode.STANDARD, SyncScope())
        )

    def critical_market(self, mode: TushareMode) -> tuple[RequestPlan, ...]:
        scope = SyncScope(trade_dates=(self.business_date,))
        return tuple(
            plan
            for dataset in CRITICAL_MARKET_DATASETS
            for plan in build_request_plans(dataset, mode, scope)
        )

    def discovery(self, mode: TushareMode) -> tuple[RequestPlan, ...]:
        trade_dates = tuple(
            self.window_start + timedelta(days=offset)
            for offset in range(self.lookback_days)
        )
        disclosure_plans = build_request_plans(
            Dataset.DISCLOSURE_DATE,
            mode,
            SyncScope(trade_dates=trade_dates),
        )
        corporate_scope = SyncScope(start_date=self.window_start, end_date=self.business_date)
        corporate_plans = (
            plan
            for dataset in CORPORATE_WINDOW_DATASETS
            for plan in build_request_plans(dataset, mode, corporate_scope)
        )
        return (*disclosure_plans, *corporate_plans)

    def financial(
        self,
        mode: TushareMode,
        symbols: Iterable[str],
        periods: Iterable[date],
    ) -> tuple[RequestPlan, ...]:
        scope = SyncScope(
            symbols=self.merge_symbols(symbols),
            periods=self.merge_periods(periods),
        )
        return tuple(
            plan
            for dataset in FINANCIAL_DATASETS
            for plan in build_request_plans(dataset, mode, scope)
        )

    @staticmethod
    def merge_symbols(*groups: Iterable[str]) -> tuple[str, ...]:
        return tuple(sorted({symbol for group in groups for symbol in group}))

    @staticmethod
    def merge_periods(*groups: Iterable[date]) -> tuple[date, ...]:
        return tuple(sorted({period for group in groups for period in group}))
