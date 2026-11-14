from __future__ import annotations

from datetime import UTC, date, datetime
import json

import httpx
import pytest
from sqlalchemy import select

from app.connectors.macro.contracts import MacroFetchWindow
from app.connectors.macro.tushare import TushareMacroProvider
from app.connectors.tushare.client import TushareClient
from app.db.base import Base, ExternalBase
from app.db.models import IngestionBatch, MacroObservation, SourcePayload
from app.db.session import create_engine_and_session
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


@pytest.mark.parametrize("max_retries", [0, 2])
@pytest.mark.parametrize("failure,error_code,retryable", [
    ("503", "TUSHARE_TRANSIENT_FAILURE", True),
    ("auth", "TUSHARE_AUTH_FAILED", False),
    ("permission", "TUSHARE_PERMISSION_DENIED", False),
    ("schema", "TUSHARE_SCHEMA_INVALID", False),
    ("value", "TUSHARE_VALUE_INVALID", False),
    ("date", "TUSHARE_DATE_INVALID", False),
    ("conflict", "TUSHARE_MIXED_SERIES", False),
])
def test_later_window_failure_retains_sanitized_prior_response_in_real_sqlite_audit(
    failure: str, error_code: str, retryable: bool, max_retries: int,
) -> None:
    # 捕获的错误：后片失败清空前片raw，或解析失败清空已取得的分片身份。
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("us.rate.treasury_10y")
    source = next(item for item in series.sources if item.provider == "tushare")
    window = MacroFetchWindow(date(2025, 1, 1), date(2026, 1, 2), datetime(2026, 1, 3, tzinfo=UTC))
    token = "synthetic-residual-raw-token"
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if body["params"]["start_date"] == "20250101":
            # 响应额外包含敏感字段，不能进入provider输出或审计库。
            return httpx.Response(200, json={"code": 0, "msg": "", "data": {
                "fields": ["date", "y10", "token", "Authorization"],
                "items": [["20250102", "4.5", token, f"Bearer {token}"]],
            }})
        if failure == "503":
            return httpx.Response(503, text=f"token={token}")
        if failure in {"auth", "permission"}:
            message = f"token {token} invalid" if failure == "auth" else f"权限不足 {token}"
            return httpx.Response(200, json={"code": -1, "msg": message, "data": None})
        if failure == "schema":
            return httpx.Response(200, json={"code": 0, "data": {"fields": ["date", "y10"], "items": [["20260102"]]}})
        row = {
            "value": ["20260102", "invalid-value"],
            "date": ["invalid-date", "4.6"],
            "conflict": ["20250102", "4.6"],
        }[failure]
        return httpx.Response(200, json={"code": 0, "msg": "", "data": {
            "fields": ["date", "y10"], "items": [row],
        }})

    with TushareClient(token, transport=httpx.MockTransport(handler), max_retries=max_retries, backoff_seconds=0) as client:
        result = TushareMacroProvider(client).fetch(series, source, window)

    assert result.status == "failed"
    assert result.observations == ()
    assert result.request_count == 2
    assert result.error is not None
    assert result.error.code == error_code
    assert result.error.retryable is retryable
    assert result.error.request_count == 2
    expected_second_attempts = max_retries + 1 if failure == "503" else 1
    assert [body["params"] for body in sent] == [
        {"start_date": "20250101", "end_date": "20251231"},
        *[{"start_date": "20260101", "end_date": "20260102"}] * expected_second_attempts,
    ]
    assert all(body["api_name"] == "us_tycr" and body["fields"] == "date,y10" for body in sent)

    engine, factory = create_engine_and_session("sqlite+pysqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        ExternalBase.metadata.create_all(engine)
        repository = MacroObservationRepository(factory)
        batch_id = repository.audit_request(series, source, window, result, window.as_of, "residual-raw-run")
        assert repository.audit_request(series, source, window, result, window.as_of, "residual-raw-run") == batch_id
        with factory() as session:
            batch = session.get(IngestionBatch, batch_id)
            payloads = session.scalars(select(SourcePayload)).all()
            assert batch is not None and batch.status == "failed"
            assert batch.request_count == 2 and batch.row_count == 0
            assert len(payloads) == 1 and payloads[0].batch_id == batch_id
            payload = payloads[0].payload
            assert payload["status"] == "failed" and payload["requestCount"] == 2
            assert payload["request"]["window"] == {
                "start": "2025-01-01", "end": "2026-01-02", "asOf": "2026-01-03T00:00:00+00:00",
            }
            assert payload["request"]["seriesId"] == "us.rate.treasury_10y"
            assert payload["data"]["rows"][0] == {"date": "20250102", "y10": "4.5"}
            parts = payload["data"]["requests"]
            assert len(parts) == 2
            assert parts[0]["rows"] == [{"date": "20250102", "y10": "4.5"}]
            assert [part["params"] for part in parts] == [
                {"start_date": "20250101", "end_date": "20251231"},
                {"start_date": "20260101", "end_date": "20260102"},
            ]
            assert [part["window"] for part in parts] == [
                {"start": "2025-01-01", "end": "2025-12-31", "asOf": "2026-01-03T00:00:00+00:00"},
                {"start": "2026-01-01", "end": "2026-01-02", "asOf": "2026-01-03T00:00:00+00:00"},
            ]
            for part in parts:
                assert part["apiName"] == "us_tycr" and part["field"] == "y10"
                assert part["responseFields"] == ["date", "y10"]
                assert part["sourceUrl"] == str(source.source_url)
            assert parts[0]["status"] == "success"
            if failure in {"503", "auth", "permission", "schema"}:
                assert parts[1]["status"] == "failed"
                assert parts[1]["errorCode"] == error_code
                assert parts[1]["rows"] == []
                assert payload["data"]["rows"] == parts[0]["rows"]
            else:
                assert parts[1]["status"] == "success"  # 已解码正文，观测校验随后失败。
                expected_row = {
                    "value": {"date": "20260102", "y10": "invalid-value"},
                    "date": {"date": "invalid-date", "y10": "4.6"},
                    "conflict": {"date": "20250102", "y10": "4.6"},
                }[failure]
                assert parts[1]["rows"] == [expected_row]
                assert payload["data"]["rows"] == [*parts[0]["rows"], *parts[1]["rows"]]
            assert token not in json.dumps(payload)
            assert token not in repr(result.raw_payload) + str(result.error)
            assert session.scalars(select(MacroObservation)).all() == []
    finally:
        engine.dispose()
