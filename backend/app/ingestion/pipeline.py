from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.strategies import Dataset, RequestPlan
from app.db.models import AdjustmentFactor, BalanceSheet, BusinessSegment, CashflowStatement, Classification, DailyBasic, DailyPrice, DisclosureSchedule, Dividend, FinancialExpress, FinancialForecast, FinancialIndicator, IncomeStatement, Repurchase, Security, ShareUnlock, ShareholderCount, TopHolder, TradeCalendar
from app.ingestion.normalizers import normalize_adjustment_factor, normalize_classification, normalize_daily_basic, normalize_daily_price, normalize_disclosure_schedule, normalize_dividend, normalize_event_row, normalize_financial_row, normalize_security, normalize_trade_calendar
from app.ingestion.repositories import IngestionRepository
from app.stocks.universes.filter import FilteredResponse, UniverseRowFilter


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    dataset: Dataset
    received_count: int
    row_count: int
    rejected_count: int
    retained_count: int = -1
    filtered_count: int = 0

    def __post_init__(self) -> None:
        if self.retained_count == -1:
            object.__setattr__(self, "retained_count", self.received_count)
        elif self.retained_count < 0:
            raise ValueError("retained_count must not be negative")
        if self.filtered_count < 0:
            raise ValueError("filtered_count must not be negative")


class IngestionPipeline:
    def __init__(
        self,
        client: TushareClient,
        session_factory: sessionmaker[Session],
        *,
        row_filter: UniverseRowFilter | None = None,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._row_filter = row_filter

    def run(self, plan: RequestPlan, *, mode: str | None = None) -> BatchResult:
        provider_response = self._client.call(plan.api_name, plan.params, plan.fields)
        if self._row_filter is None:
            filtered = FilteredResponse(
                response=provider_response,
                provider_count=len(provider_response.items),
                retained_count=len(provider_response.items),
                filtered_count=0,
            )
        else:
            filtered = self._row_filter.filter(plan.dataset, provider_response)
        response = filtered.response
        with self._session_factory.begin() as session:
            return self.persist_response(plan, response, session=session, mode=mode,
                received_count=filtered.provider_count, filtered_count=filtered.filtered_count)

    def persist_response(self, plan: RequestPlan, response: TushareResponse, *, session: Session,
        mode: str | None = None, raw_response: TushareResponse | None = None,
        received_count: int | None = None, filtered_count: int = 0) -> BatchResult:
        """调用方拥有事务；本方法不提交，用于core/raw/cursor原子写入。"""
        repository = IngestionRepository(session)
        batch = repository.start_batch(plan.dataset.value, mode)
        repository.save_payload(batch, plan.api_name, plan.params, plan.fields, raw_response or response)
        rejected = 0
        for row_number, row in enumerate(response.rows(), start=1):
            try:
                with session.begin_nested():
                    if plan.dataset is Dataset.SHARE_UNLOCK and "unlock_date" not in plan.fields:
                        # 原件已完整保留；供应商意外返回的退役字段不能覆盖旧值。
                        row = {key: value for key, value in row.items() if key != "unlock_date"}
                    self._upsert_row(repository, plan.dataset, row, classification_source=plan.params.get("src"))
            except (KeyError, TypeError, ValueError, DataError, IntegrityError) as exc:
                rejected += 1
                repository.reject(batch, str(exc), row_number=row_number, raw_record=row)
        batch.request_count = 1
        batch.row_count = len(response.items) - rejected
        batch.rejected_count = rejected
        batch.status = "completed" if rejected == 0 else "partial"
        batch.finished_at = datetime.now(timezone.utc)
        return BatchResult(batch.id, plan.dataset,
            len(response.items) if received_count is None else received_count,
            batch.row_count, rejected, len(response.items), filtered_count)

    def _upsert_row(self, repository: IngestionRepository, dataset: Dataset, row: dict[str, Any], *, classification_source: str | None = None) -> None:
        if dataset is Dataset.SECURITY:
            normalized = normalize_security(row)
            repository.upsert(Security, {"ts_code": normalized.pop("ts_code")}, normalized)
        elif dataset is Dataset.DAILY:
            normalized = normalize_daily_price(row)
            repository.upsert(
                DailyPrice,
                {"ts_code": normalized.pop("ts_code"), "trade_date": normalized["trade_date"]},
                normalized,
            )
        elif dataset is Dataset.DAILY_BASIC:
            normalized = normalize_daily_basic(row)
            repository.upsert(
                DailyBasic,
                {"ts_code": normalized.pop("ts_code"), "trade_date": normalized["trade_date"]},
                normalized,
            )
        elif dataset is Dataset.INCOME:
            normalized = normalize_financial_row(row)
            repository.upsert(
                IncomeStatement,
                {
                    "ts_code": normalized["ts_code"],
                    "end_date": normalized["end_date"],
                    "report_type": normalized["report_type"],
                    "ann_date": normalized["ann_date"],
                    "update_flag": normalized["update_flag"],
                },
                normalized,
            )
        elif dataset is Dataset.BALANCE_SHEET:
            normalized = normalize_financial_row(row)
            repository.upsert(BalanceSheet, {"ts_code": normalized["ts_code"], "end_date": normalized["end_date"], "report_type": normalized["report_type"], "ann_date": normalized["ann_date"], "update_flag": normalized["update_flag"]}, normalized)
        elif dataset is Dataset.CASHFLOW:
            normalized = normalize_financial_row(row)
            repository.upsert(CashflowStatement, {"ts_code": normalized["ts_code"], "end_date": normalized["end_date"], "report_type": normalized["report_type"], "ann_date": normalized["ann_date"], "update_flag": normalized["update_flag"]}, normalized)
        elif dataset is Dataset.FINANCIAL_INDICATOR:
            normalized = normalize_financial_row(row)
            repository.upsert(FinancialIndicator, {"ts_code": normalized["ts_code"], "end_date": normalized["end_date"], "report_type": normalized["report_type"], "ann_date": normalized["ann_date"], "update_flag": normalized["update_flag"]}, normalized)
        elif dataset is Dataset.FINANCIAL_FORECAST:
            normalized = normalize_financial_row(row)
            repository.upsert(FinancialForecast, {"ts_code": normalized["ts_code"], "end_date": normalized["end_date"], "report_type": normalized["report_type"], "ann_date": normalized["ann_date"], "update_flag": normalized["update_flag"]}, normalized)
        elif dataset is Dataset.FINANCIAL_EXPRESS:
            normalized = normalize_financial_row(row)
            repository.upsert(FinancialExpress, {"ts_code": normalized["ts_code"], "end_date": normalized["end_date"], "report_type": normalized["report_type"], "ann_date": normalized["ann_date"], "update_flag": normalized["update_flag"]}, normalized)
        elif dataset is Dataset.TRADE_CALENDAR:
            normalized = normalize_trade_calendar(row)
            repository.upsert(TradeCalendar, {"exchange": normalized.pop("exchange"), "cal_date": normalized["cal_date"]}, normalized)
        elif dataset is Dataset.DISCLOSURE_DATE:
            normalized = normalize_disclosure_schedule(row)
            repository.upsert(
                DisclosureSchedule,
                {
                    "ts_code": normalized["ts_code"],
                    "end_date": normalized["end_date"],
                },
                normalized,
            )
        elif dataset is Dataset.ADJ_FACTOR:
            normalized = normalize_adjustment_factor(row)
            repository.upsert(AdjustmentFactor, {"ts_code": normalized.pop("ts_code"), "trade_date": normalized["trade_date"]}, normalized)
        elif dataset is Dataset.DIVIDEND:
            normalized = normalize_dividend(row)
            repository.upsert(Dividend, {"ts_code": normalized["ts_code"], "end_date": normalized["end_date"], "ann_date": normalized["ann_date"], "div_proc": normalized["div_proc"]}, normalized)
        elif dataset is Dataset.REPURCHASE:
            normalized = normalize_event_row(row)
            values = {key: normalized.get(key) for key in ("ann_date", "proc", "data")}
            repository.upsert(Repurchase, {"ts_code": normalized["ts_code"], "ann_date": values["ann_date"], "proc": values["proc"]}, values)
        elif dataset is Dataset.SHARE_UNLOCK:
            normalized = normalize_event_row(row)
            values = {key: normalized.get(key) for key in ("ann_date", "float_date", "data")}
            repository.upsert(ShareUnlock, {"ts_code": normalized["ts_code"], "ann_date": values["ann_date"]}, values)
        elif dataset is Dataset.SHAREHOLDER_COUNT:
            normalized = normalize_event_row(row)
            values = {key: normalized.get(key) for key in ("ann_date", "end_date", "holder_num", "data")}
            repository.upsert(ShareholderCount, {"ts_code": normalized["ts_code"], "end_date": values["end_date"]}, values)
        elif dataset is Dataset.TOP_HOLDER:
            normalized = normalize_event_row(row)
            values = {key: normalized.get(key) for key in ("ann_date", "end_date", "holder_type", "holder_name", "hold_amount", "data")}
            repository.upsert(TopHolder, {"ts_code": normalized["ts_code"], "end_date": values["end_date"], "holder_name": values["holder_name"]}, values)
        elif dataset is Dataset.BUSINESS_SEGMENT:
            normalized = normalize_event_row(row)
            values = {key: normalized.get(key) for key in ("end_date", "segment_type", "segment_name", "revenue", "data")}
            repository.upsert(BusinessSegment, {"ts_code": normalized["ts_code"], "end_date": values["end_date"], "segment_name": values["segment_name"]}, values)
        elif dataset is Dataset.CLASSIFICATION:
            normalized = normalize_classification(row, source=classification_source)
            repository.upsert(
                Classification,
                {
                    "provider": "tushare",
                    "classification_id": normalized["classification_id"],
                    "source": normalized["source"],
                },
                normalized,
            )
        else:
            raise ValueError(f"dataset {dataset.value} has no pipeline normalizer")
