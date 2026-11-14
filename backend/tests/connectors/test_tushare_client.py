from __future__ import annotations

import httpx
import pytest

from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.errors import (
    TushareAuthError,
    TushareNetworkError,
    TusharePermissionError,
    TushareRateLimitError,
    TushareResponseError,
    TushareSchemaError,
    TushareServerError,
)


class RecordingLimiter:
    def __init__(self) -> None:
        self.calls = 0

    def acquire(self) -> None:
        self.calls += 1


def response(
    *,
    code: int = 0,
    message: str = "",
    fields: list[str] | None = None,
    items: list[list[object]] | None = None,
    status_code: int = 200,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={
            "code": code,
            "msg": message,
            "data": {"fields": fields or ["ts_code"], "items": items or [["600519.SH"]]},
        },
    )


def test_client_maps_fields_and_items() -> None:
    transport = httpx.MockTransport(
        lambda request: response(fields=["ts_code", "name"], items=[["600519.SH", "贵州茅台"]])
    )
    client = TushareClient("secret-token", transport=transport)

    result = client.call("stock_basic", {}, ["ts_code", "name"])

    assert result.rows() == ({"ts_code": "600519.SH", "name": "贵州茅台"},)


def test_client_rejects_missing_token() -> None:
    with pytest.raises(TushareAuthError):
        TushareClient("").call("daily", {}, [])


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("无权限访问该接口", TusharePermissionError),
        ("token无效", TushareAuthError),
        ("每分钟最多访问该接口 1 次", TushareRateLimitError),
    ],
)
def test_client_classifies_vendor_errors(
    message: str,
    error: type[Exception],
) -> None:
    transport = httpx.MockTransport(lambda request: response(code=-1, message=message))
    client = TushareClient("secret-token", transport=transport)

    with pytest.raises(error):
        client.call("daily", {}, [])


def test_client_retries_server_error_then_succeeds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(status_code=503)
        return response()

    client = TushareClient(
        "secret-token",
        transport=httpx.MockTransport(handler),
        max_retries=1,
        backoff_seconds=0,
    )

    assert client.call("daily", {}, []).rows() == ({"ts_code": "600519.SH"},)
    assert calls == 2


def test_client_acquires_rate_limiter_before_immediate_success() -> None:
    limiter = RecordingLimiter()
    client = TushareClient(
        "secret-token",
        transport=httpx.MockTransport(lambda request: response()),
        rate_limiter=limiter,
    )

    assert client.call("daily", {}, []).rows() == ({"ts_code": "600519.SH"},)
    assert limiter.calls == 1


@pytest.mark.parametrize("failure_kind", ["network", "http_429", "http_503"])
def test_client_acquires_rate_limiter_for_each_retriable_attempt(
    failure_kind: str,
) -> None:
    limiter = RecordingLimiter()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            if failure_kind == "network":
                raise httpx.ConnectError("offline", request=request)
            if failure_kind == "http_429":
                return response(status_code=429)
            return response(status_code=503)
        return response()

    client = TushareClient(
        "secret-token",
        transport=httpx.MockTransport(handler),
        max_retries=2,
        backoff_seconds=0,
        rate_limiter=limiter,
    )

    assert client.call("daily", {}, []).rows() == ({"ts_code": "600519.SH"},)
    assert limiter.calls == 3


def test_client_stops_acquiring_rate_limiter_after_non_retriable_response() -> None:
    limiter = RecordingLimiter()
    client = TushareClient(
        "secret-token",
        transport=httpx.MockTransport(lambda request: response(status_code=400)),
        max_retries=2,
        rate_limiter=limiter,
    )

    with pytest.raises(TushareResponseError):
        client.call("daily", {}, [])

    assert limiter.calls == 1


@pytest.mark.parametrize(
    ("status_code", "error"),
    [(429, TushareRateLimitError), (503, TushareServerError)],
)
def test_client_classifies_exhausted_http_errors(
    status_code: int,
    error: type[Exception],
) -> None:
    client = TushareClient(
        "secret-token",
        transport=httpx.MockTransport(lambda request: response(status_code=status_code)),
        max_retries=0,
    )

    with pytest.raises(error):
        client.call("daily", {}, [])


def test_client_classifies_exhausted_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = TushareClient(
        "secret-token",
        transport=httpx.MockTransport(handler),
        max_retries=0,
    )

    with pytest.raises(TushareNetworkError):
        client.call("daily", {}, [])


def test_client_rejects_mismatched_fields_and_items() -> None:
    transport = httpx.MockTransport(
        lambda request: response(fields=["ts_code", "name"], items=[["600519.SH"]])
    )
    client = TushareClient("secret-token", transport=transport)

    with pytest.raises(TushareSchemaError):
        client.call("stock_basic", {}, ["ts_code", "name"])
