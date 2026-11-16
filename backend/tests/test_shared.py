from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.models import PublishedSnapshot
from backend.tests.support.application import create_app
from app.shared.contracts import DataMeta, success_result
from app.shared.errors import DataServiceError, error_payload
from app.shared.repository import SnapshotRepository


def test_success_result_preserves_stable_meta_keys() -> None:
    meta = DataMeta(
        source="fixture",
        asOf="2026-08-15T00:00:00+00:00",
        version="fixture-v1",
        stale=False,
    )
    assert success_result({"value": 1}, meta).model_dump(by_alias=True) == {
        "data": {"value": 1},
        "meta": {
            "source": "fixture",
            "asOf": "2026-08-15T00:00:00+00:00",
            "version": "fixture-v1",
            "stale": False,
        },
    }


def test_data_meta_exposes_optional_stale_reason_alias() -> None:
    meta = DataMeta(
        source="database",
        asOf="2026-09-05T00:00:00+00:00",
        version="v1",
        stale=True,
        staleReason="SNAPSHOT_BEHIND_EXPECTED_DATE",
    )

    assert meta.stale_reason == "SNAPSHOT_BEHIND_EXPECTED_DATE"
    assert meta.model_dump(by_alias=True)["staleReason"] == (
        "SNAPSHOT_BEHIND_EXPECTED_DATE"
    )


def test_snapshot_repository_exposes_stale_reason(pg_factory) -> None:
    with pg_factory.begin() as session:
        session.add(
            PublishedSnapshot(
                product="market-overview",
                version="v1",
                as_of=datetime(2026, 9, 5, tzinfo=timezone.utc),
                payload={"market": "overview"},
                stale=True,
                stale_reason="SNAPSHOT_BEHIND_EXPECTED_DATE",
                freshness_checked_at=datetime(
                    2026, 9, 5, 4, 0, tzinfo=timezone.utc
                ),
                published_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
            )
        )

    snapshot = SnapshotRepository(pg_factory).read("market-overview")

    assert snapshot.meta.stale_reason == "SNAPSHOT_BEHIND_EXPECTED_DATE"
    assert snapshot.meta.model_dump(by_alias=True)["staleReason"] == (
        "SNAPSHOT_BEHIND_EXPECTED_DATE"
    )


def test_error_payload_does_not_include_internal_cause() -> None:
    error = DataServiceError("DATA_SOURCE_UNAVAILABLE", "数据源不可用")
    payload = error_payload(error, "request-1")
    assert payload == {
        "error": {"code": "DATA_SOURCE_UNAVAILABLE", "message": "数据源不可用"},
        "meta": {"requestId": "request-1"},
    }


def test_health_and_local_cors() -> None:
    client = TestClient(create_app())
    assert client.get("/health").json() == {"status": "ok"}
    response = client.options(
        "/health",
        headers={
            "origin": "http://localhost:3000",
            "access-control-request-method": "GET",
        },
    )
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
