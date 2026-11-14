from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.tushare.client import TushareClient
from app.db.base import Base
from app.db.models import (
    BalanceSheet,
    BusinessSegment,
    CashflowStatement,
    Classification,
    DailyPrice,
    DisclosureSchedule,
    Dividend,
    FinancialExpress,
    FinancialForecast,
    FinancialIndicator,
    IncomeStatement,
    IngestionBatch,
    JobRun,
    RejectedRecord,
    Repurchase,
    Security,
    ShareUnlock,
    ShareholderCount,
    SourcePayload,
    TopHolder,
)
from app.ingestion.pipeline import BatchResult, IngestionPipeline
from app.connectors.tushare.strategies import Dataset, RequestPlan, SyncScope, TushareMode, build_request_plans
from app.stocks.universes.filter import UniverseRowFilter
from app.stocks.universes.models import UniverseSelection


def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        execution_options={"schema_translate_map": {"raw": None, "core": None, "ops": None}},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(engine, expire_on_commit=False)


def test_batch_result_defaults_retained_count_to_provider_count() -> None:
    result = BatchResult("batch-1", Dataset.DAILY, 3, 3, 0)

    assert result.retained_count == 3
    assert result.filtered_count == 0


def test_pipeline_saves_raw_and_upserts_daily_price() -> None:
    engine, factory = session_factory()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "fields": ["ts_code", "symbol", "name", "exchange", "list_date", "market", "list_status"],
                    "items": [["600519.SH", "600519", "贵州茅台", "SSE", "20010827", "主板", "L"]],
                },
            },
        )
    )
    client = TushareClient("token", transport=transport)
    pipeline = IngestionPipeline(client, factory)

    plan = RequestPlan(
        dataset=Dataset.SECURITY,
        api_name="stock_basic",
        params={},
        fields=("ts_code", "symbol", "name", "exchange", "list_date", "market", "list_status"),
        cursor_key="security:all",
    )

    first = pipeline.run(plan)
    second = pipeline.run(plan)

    with factory() as session:
        assert session.scalar(select(Security.ts_code)) == "600519.SH"
        assert session.scalar(select(IngestionBatch).order_by(IngestionBatch.started_at.desc()).limit(1)) is not None
        assert session.scalar(select(SourcePayload.id)) is not None

    assert first.received_count == 1
    assert first.row_count == 1
    assert first.rejected_count == 0
    assert second.received_count == 1
    assert second.row_count == 1
    assert second.rejected_count == 0
    engine.dispose()


def test_pipeline_is_idempotent_and_keeps_revised_raw_payload() -> None:
    engine, factory = session_factory()
    responses = ["10.00", "10.00", "11.00"]

    def handler(_request: httpx.Request) -> httpx.Response:
        close = responses.pop(0)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "fields": [
                        "ts_code",
                        "trade_date",
                        "open",
                        "high",
                        "low",
                        "close",
                        "pre_close",
                        "change",
                        "pct_chg",
                        "vol",
                        "amount",
                    ],
                    "items": [[
                        "600519.SH",
                        "20260817",
                        "9.50",
                        "11.20",
                        "9.10",
                        close,
                        "9.80",
                        "0.20",
                        "2.04",
                        "100",
                        "1000",
                    ]],
                },
            },
        )

    pipeline = IngestionPipeline(
        TushareClient("token", transport=httpx.MockTransport(handler)),
        factory,
    )
    plan = RequestPlan(
        dataset=Dataset.DAILY,
        api_name="daily",
        params={"ts_code": "600519.SH"},
        fields=(
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "pre_close",
            "change",
            "pct_chg",
            "vol",
            "amount",
        ),
        cursor_key="daily:600519.SH",
    )

    pipeline.run(plan, mode="standard")
    pipeline.run(plan, mode="standard")
    revised = pipeline.run(plan, mode="standard")

    with factory() as session:
        price = session.scalar(select(DailyPrice))
        assert session.scalar(select(func.count(DailyPrice.ts_code))) == 1
        assert session.scalar(select(func.count(IngestionBatch.id))) == 3
        assert session.scalar(select(func.count(SourcePayload.id))) == 3
        assert session.scalar(
            select(func.count(func.distinct(SourcePayload.content_hash)))
        ) == 2
        assert set(session.scalars(select(SourcePayload.batch_id))) == set(
            session.scalars(select(IngestionBatch.id))
        )
        assert set(session.scalars(select(IngestionBatch.mode))) == {"standard"}

    assert price is not None
    assert revised.received_count == 1
    assert price.close == Decimal("11.00")
    assert revised.row_count == 1
    assert revised.rejected_count == 0
    engine.dispose()


def test_pipeline_records_invalid_row_as_rejected() -> None:
    engine, factory = session_factory()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "fields": ["ts_code", "trade_date", "close"],
                    "items": [["600519.SH", "not-a-date", "10.00"]],
                },
            },
        )
    )
    pipeline = IngestionPipeline(TushareClient("token", transport=transport), factory)
    plan = RequestPlan(
        dataset=Dataset.DAILY,
        api_name="daily",
        params={"ts_code": "600519.SH"},
        fields=("ts_code", "trade_date", "close"),
        cursor_key="daily:600519.SH",
    )

    result = pipeline.run(plan, mode="standard")

    with factory() as session:
        batch = session.get(IngestionBatch, result.batch_id)
        rejected = session.scalar(select(RejectedRecord))

    assert result.received_count == 1
    assert result.row_count == 0
    assert result.rejected_count == 1
    assert batch is not None
    assert batch.status == "partial"
    assert rejected is not None
    assert rejected.row_number == 1
    assert rejected.raw_record == {
        "ts_code": "600519.SH",
        "trade_date": "not-a-date",
        "close": "10.00",
    }
    engine.dispose()


def test_pipeline_normalizes_official_index_classification() -> None:
    engine, factory = session_factory()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "fields": [
                        "index_code",
                        "industry_name",
                        "parent_code",
                        "level",
                        "industry_code",
                        "is_pub",
                        "src",
                    ],
                    "items": [[
                        "801120.SI",
                        "食品饮料",
                        "0",
                        "L1",
                        "710000",
                        "1",
                        "SW2021",
                    ]],
                },
            },
        )
    )
    pipeline = IngestionPipeline(TushareClient("token", transport=transport), factory)
    plan = RequestPlan(
        dataset=Dataset.CLASSIFICATION,
        api_name="index_classify",
        params={},
        fields=(
            "index_code",
            "industry_name",
            "parent_code",
            "level",
            "industry_code",
            "is_pub",
            "src",
        ),
        cursor_key="classification:all",
    )

    result = pipeline.run(plan, mode="standard")

    with factory() as session:
        classification = session.scalar(select(Classification))

    assert result.row_count == 1
    assert result.rejected_count == 0
    assert classification is not None
    assert classification.classification_id == "801120.SI"
    assert classification.name == "食品饮料"
    assert classification.level == 1
    assert classification.parent_id == "0"
    assert classification.data == {
        "index_code": "801120.SI",
        "industry_name": "食品饮料",
        "parent_code": 0,
        "level": "L1",
        "industry_code": 710000,
        "is_pub": 1,
        "src": "SW2021",
    }
    engine.dispose()


def test_pipeline_uses_savepoint_for_database_rejected_row() -> None:
    engine, factory = session_factory()
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="贵州茅台"))

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "fields": ["ts_code", "trade_date", "close"],
                    "items": [
                        ["600519.SH", "20260817", "10.00"],
                        ["999999.SH", "20260817", "8.00"],
                    ],
                },
            },
        )
    )
    pipeline = IngestionPipeline(TushareClient("token", transport=transport), factory)
    plan = RequestPlan(
        dataset=Dataset.DAILY,
        api_name="daily",
        params={"trade_date": "20260817"},
        fields=("ts_code", "trade_date", "close"),
        cursor_key="daily:20260817",
    )

    result = pipeline.run(plan, mode="standard")

    with factory() as session:
        assert session.scalar(select(func.count(DailyPrice.id))) == 1
        assert session.scalar(select(func.count(SourcePayload.id))) == 1
        rejected = session.scalar(select(RejectedRecord))
        batch = session.get(IngestionBatch, result.batch_id)

    assert result.received_count == 2
    assert result.row_count == 1
    assert result.rejected_count == 1
    assert batch is not None
    assert batch.status == "partial"
    assert rejected is not None
    assert "FOREIGN KEY constraint failed" in rejected.reason
    assert rejected.raw_record is not None
    assert rejected.raw_record["ts_code"] == "999999.SH"
    engine.dispose()


def test_pipeline_tracks_empty_response_counts_and_completed_batch() -> None:
    engine, factory = session_factory()
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "fields": ["ts_code", "trade_date", "close"],
                    "items": [],
                },
            },
        )
    )
    pipeline = IngestionPipeline(TushareClient("token", transport=transport), factory)
    plan = RequestPlan(
        dataset=Dataset.DAILY,
        api_name="daily",
        params={"ts_code": "600519.SH"},
        fields=("ts_code", "trade_date", "close"),
        cursor_key="daily:600519.SH",
    )

    result = pipeline.run(plan, mode="standard")

    with factory() as session:
        batch = session.get(IngestionBatch, result.batch_id)
        payload_count = session.scalar(select(func.count(SourcePayload.id)))
        batch_count = session.scalar(select(func.count(IngestionBatch.id)))

    assert result.received_count == 0
    assert result.row_count == 0
    assert result.rejected_count == 0
    assert batch is not None
    assert batch.status == "completed"
    assert payload_count == 1
    assert batch_count == 1
    engine.dispose()


def test_pipeline_filters_before_raw_and_core_persistence() -> None:
    engine, factory = session_factory()
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=ON"))
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="贵州茅台"))

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "fields": ["ts_code", "trade_date", "close"],
                    "items": [
                        ["600519.SH", "20260904", "10.00"],
                        ["000001.SZ", "20260904", "9.00"],
                        [None, "20260904", "8.00"],
                    ],
                },
            },
        )
    )
    selected = UniverseSelection(
        profile="representative-50",
        version="2026-09-04-v1",
        coverage="representative",
        symbols=("600519.SH",),
        fingerprint="test-fingerprint",
    )
    pipeline = IngestionPipeline(
        TushareClient("token", transport=transport),
        factory,
        row_filter=UniverseRowFilter(selected),
    )
    plan = RequestPlan(
        dataset=Dataset.DAILY,
        api_name="daily",
        params={"trade_date": "20260904"},
        fields=("ts_code", "trade_date", "close"),
        cursor_key="daily:20260904",
    )

    result = pipeline.run(plan, mode="standard")

    with factory() as session:
        payload = session.scalar(select(SourcePayload))
        rejected = session.scalar(select(RejectedRecord))
        symbols = tuple(session.scalars(select(DailyPrice.ts_code)))

    assert result.received_count == 3
    assert result.retained_count == 2
    assert result.filtered_count == 1
    assert result.row_count == 1
    assert result.rejected_count == 1
    assert payload is not None
    assert payload.payload["items"] == [
        ["600519.SH", "20260904", "10.00"],
        [None, "20260904", "8.00"],
    ]
    assert symbols == ("600519.SH",)
    assert rejected is not None
    assert rejected.raw_record is not None
    assert rejected.raw_record["ts_code"] is None
    engine.dispose()


def test_daily_sync_models_register_expected_metadata() -> None:
    disclosure_table = DisclosureSchedule.__table__
    unique_columns = {
        tuple(constraint.columns.keys())
        for constraint in disclosure_table.constraints
        if constraint.name == "uq_disclosure_schedule_period"
    }

    assert disclosure_table.schema == "core"
    assert unique_columns == {("ts_code", "end_date")}
    assert ShareholderCount.__table__.c.ann_date.nullable is True
    assert JobRun.__table__.c.details.nullable is False
    assert JobRun.__table__.c.details.default is not None
    assert JobRun.__table__.c.details.server_default is not None


def test_pipeline_upserts_revised_disclosure_schedule_by_period() -> None:
    engine, factory = session_factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="贵州茅台"))

    actual_dates = ["20260901", "20260902"]

    def handler(_request: httpx.Request) -> httpx.Response:
        actual_date = actual_dates.pop(0)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "fields": [
                        "ts_code",
                        "ann_date",
                        "end_date",
                        "pre_date",
                        "actual_date",
                        "modify_date",
                    ],
                    "items": [[
                        "600519.SH",
                        "20260820",
                        "20260630",
                        "20260830",
                        actual_date,
                        "20260831",
                    ]],
                },
            },
        )

    pipeline = IngestionPipeline(
        TushareClient("token", transport=httpx.MockTransport(handler)),
        factory,
    )
    plan = RequestPlan(
        dataset=Dataset.DISCLOSURE_DATE,
        api_name="disclosure_date",
        params={"actual_date": "20260902"},
        fields=(
            "ts_code",
            "ann_date",
            "end_date",
            "pre_date",
            "actual_date",
            "modify_date",
        ),
        cursor_key="disclosure_date:actual:20260902",
    )

    assert pipeline.run(plan, mode="standard").rejected_count == 0
    assert pipeline.run(plan, mode="standard").rejected_count == 0

    with factory() as session:
        disclosure = session.scalar(select(DisclosureSchedule))
        count = session.scalar(select(func.count(DisclosureSchedule.id)))

    assert count == 1
    assert disclosure is not None
    assert disclosure.ann_date == date(2026, 8, 20)
    assert disclosure.end_date == date(2026, 6, 30)
    assert disclosure.pre_date == date(2026, 8, 30)
    assert disclosure.actual_date == date(2026, 9, 2)
    assert disclosure.modify_date == date(2026, 8, 31)
    assert disclosure.data["actual_date"] == "20260902"
    engine.dispose()


def test_pipeline_upserts_financial_and_corporate_action_rows_with_revisions() -> None:
    engine, factory = session_factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="贵州茅台"))

    models = {
        Dataset.INCOME: IncomeStatement,
        Dataset.BALANCE_SHEET: BalanceSheet,
        Dataset.CASHFLOW: CashflowStatement,
        Dataset.FINANCIAL_INDICATOR: FinancialIndicator,
        Dataset.FINANCIAL_FORECAST: FinancialForecast,
        Dataset.FINANCIAL_EXPRESS: FinancialExpress,
        Dataset.DIVIDEND: Dividend,
        Dataset.REPURCHASE: Repurchase,
        Dataset.SHARE_UNLOCK: ShareUnlock,
        Dataset.SHAREHOLDER_COUNT: ShareholderCount,
        Dataset.TOP_HOLDER: TopHolder,
        Dataset.BUSINESS_SEGMENT: BusinessSegment,
    }
    revisions = {"income": "100"}

    def value_for(field: str) -> str:
        values = {
            "ts_code": "600519.SH",
            "ann_date": "20240801",
            "f_ann_date": "20240801",
            "end_date": "20240630",
            "float_date": "20250101",
            "unlock_date": "20250101",
            "record_date": "20240815",
            "ex_date": "20240816",
            "pay_date": "20240820",
            "exp_date": "20241231",
            "report_type": "1",
            "comp_type": "1",
            "update_flag": "1",
            "type": "预增",
            "div_proc": "实施",
            "proc": "实施中",
            "holder_name": "示例股东",
            "holder_type": "个人",
            "segment_name": "主营业务",
            "segment_type": "产品",
            "revenue": revisions["income"],
        }
        return values.get(field, "1")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        fields = payload["fields"].split(",")
        return httpx.Response(
            200,
            json={
                "code": 0,
                "msg": "",
                "data": {
                    "fields": fields,
                    "items": [[value_for(field) for field in fields]],
                },
            },
        )

    datasets = tuple(models)
    plans = tuple(
        plan
        for dataset in datasets
        for plan in build_request_plans(
            dataset,
            TushareMode.STANDARD,
            SyncScope(
                symbols=("600519.SH",),
                start_date=date(2024, 1, 1),
                end_date=date(2026, 8, 24),
            ),
        )
    )
    pipeline = IngestionPipeline(TushareClient("token", transport=httpx.MockTransport(handler)), factory)

    for plan in plans:
        assert pipeline.run(plan, mode="standard").rejected_count == 0
    for plan in plans:
        assert pipeline.run(plan, mode="standard").rejected_count == 0
    revisions["income"] = "200"
    pipeline.run(next(plan for plan in plans if plan.dataset is Dataset.INCOME), mode="standard")

    with factory() as session:
        assert all(session.scalar(select(func.count(model.id))) == 1 for model in models.values())
        income = session.scalar(select(IncomeStatement))
        shareholder_count = session.scalar(select(ShareholderCount))

    assert income is not None
    assert income.data["revenue"] == 200
    assert shareholder_count is not None
    assert shareholder_count.ann_date == date(2024, 8, 1)
    engine.dispose()
