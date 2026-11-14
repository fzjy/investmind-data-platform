from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import logging
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from threading import Barrier, Event

import httpx
import pytest

from app.connectors.macro.contracts import (
    ProviderAuthError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTransientError,
)
from app.connectors.macro.transport import MacroHttpTransport


@pytest.mark.parametrize("method", ["get_json", "get_text", "get_bytes", "post_json"])
@pytest.mark.parametrize("header,want", [("0", 0.0), ("date", 15.0)])
def test_all_methods_honor_zero_and_http_date_retry_after(method, header, want, monkeypatch) -> None:
    now = datetime(2026, 9, 20, tzinfo=UTC)
    import app.connectors.macro.transport as module
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(module, "datetime", FrozenDatetime, raising=False)
    retry = format_datetime(now + timedelta(seconds=15), usegmt=True) if header == "date" else header
    sleeps = []
    with MacroHttpTransport(client=_response_sequence(iter([
        httpx.Response(429, headers={"Retry-After": retry}), httpx.Response(200, json={}),
    ])), max_attempts=2, backoff_seconds=(3,), sleep=sleeps.append) as transport:
        args = ("https://example.test/data", {}) if method == "post_json" else ("https://example.test/data",)
        getattr(transport, method)(*args)
    assert sleeps == [want]


def test_unrelated_nested_response_hook_keeps_own_log_context(caplog, monkeypatch) -> None:
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)
    caplog.set_level(logging.INFO, logger="httpx")
    def hook(response):
        with _response_sequence(iter([httpx.Response(200)])) as unrelated:
            unrelated.get("https://securities.test/nested?api_key=unrelated-canary")
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
                         event_hooks={"response": [hook]})
    with MacroHttpTransport(client=client, max_attempts=1, backoff_seconds=(0,)) as transport:
        transport.get_json("https://macro.test/data?api_key=macro-canary")
    assert "unrelated-canary" in caplog.text
    assert "macro-canary" not in caplog.text


def _response_sequence(responses: Iterator[httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(lambda _: next(responses)))


def test_transport_honors_retry_after_then_returns_json() -> None:
    sleeps: list[float] = []
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    transport = MacroHttpTransport(
        client=_response_sequence(responses),
        max_attempts=2,
        backoff_seconds=(1, 3, 10),
        sleep=sleeps.append,
    )

    assert transport.get_json("https://example.test/data").payload == {"ok": True}
    assert sleeps == [2.0]


@pytest.mark.parametrize("status_code", (500, 503))
def test_transport_retries_server_failures_then_returns_json(status_code: int) -> None:
    sleeps: list[float] = []
    responses = iter([httpx.Response(status_code), httpx.Response(200, json={"ok": True})])
    transport = MacroHttpTransport(
        client=_response_sequence(responses),
        max_attempts=2,
        backoff_seconds=(1, 3, 10),
        sleep=sleeps.append,
    )

    assert transport.get_json("https://example.test/data").payload == {"ok": True}
    assert sleeps == [1.0]


def test_transport_retries_request_error_then_returns_json() -> None:
    sleeps: list[float] = []
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"ok": True})

    transport = MacroHttpTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=2,
        backoff_seconds=(1, 3, 10),
        sleep=sleeps.append,
    )

    assert transport.get_json("https://example.test/data").payload == {"ok": True}
    assert sleeps == [1.0]


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [(401, ProviderAuthError), (403, ProviderPermissionError)],
)
def test_transport_does_not_retry_auth_or_permission_errors(
    status_code: int,
    error_type: type[Exception],
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status_code)

    transport = MacroHttpTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=3,
        backoff_seconds=(1, 3, 10),
        sleep=lambda _: None,
    )

    with pytest.raises(error_type):
        transport.get_json("https://example.test/data")

    assert calls == 1


def test_transport_raises_rate_limit_error_when_retry_budget_is_exhausted() -> None:
    sleeps: list[float] = []
    transport = MacroHttpTransport(
        client=_response_sequence(iter([httpx.Response(429), httpx.Response(429)])),
        max_attempts=2,
        backoff_seconds=(1, 3, 10),
        sleep=sleeps.append,
    )

    with pytest.raises(ProviderRateLimitError) as exc_info:
        transport.get_json("https://example.test/data")

    assert exc_info.value.retryable is True
    assert sleeps == [1.0]


def test_transport_caps_retry_after_at_sixty_seconds() -> None:
    sleeps: list[float] = []
    transport = MacroHttpTransport(
        client=_response_sequence(
            iter([httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200, json={})])
        ),
        max_attempts=2,
        backoff_seconds=(1,),
        sleep=sleeps.append,
    )

    assert transport.get_json("https://example.test/data").payload == {}
    assert sleeps == [60.0]


def test_transport_rejects_invalid_json() -> None:
    transport = MacroHttpTransport(
        client=_response_sequence(iter([httpx.Response(200, content=b"not json")])),
        max_attempts=1,
        backoff_seconds=(1,),
        sleep=lambda _: None,
    )

    with pytest.raises(ProviderSchemaError):
        transport.get_json("https://example.test/data")


def test_transport_returns_public_page_text_without_json_decoding() -> None:
    transport = MacroHttpTransport(
        client=_response_sequence(
            iter(
                [
                    httpx.Response(
                        200,
                        text="<html><body>official release</body></html>",
                        headers={"Content-Type": "text/html; charset=utf-8"},
                    )
                ]
            )
        ),
        max_attempts=1,
        backoff_seconds=(1,),
        sleep=lambda _: None,
    )

    response = transport.get_text("https://example.test/release.html")

    assert response.text == "<html><body>official release</body></html>"
    assert response.request_count == 1


def test_transport_posts_public_json_without_adding_credentials() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "REQUEST_SUCCEEDED"})

    transport = MacroHttpTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=1,
        backoff_seconds=(1,),
        sleep=lambda _: None,
    )

    response = transport.post_json(
        "https://api.example.test/public",
        {"seriesid": ["SERIES"], "startyear": "2025", "endyear": "2025"},
    )

    assert response.payload == {"status": "REQUEST_SUCCEEDED"}
    assert response.request_count == 1
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].headers["content-type"] == "application/json"
    assert b"registrationkey" not in requests[0].content


def test_transport_returns_public_download_bytes() -> None:
    transport = MacroHttpTransport(
        client=_response_sequence(
            iter([httpx.Response(200, content=b"PK\x03\x04public-download")])
        ),
        max_attempts=1,
        backoff_seconds=(1,),
        sleep=lambda _: None,
    )

    response = transport.get_bytes("https://example.test/public.zip")

    assert response.content == b"PK\x03\x04public-download"
    assert response.request_count == 1


def test_transport_post_json_reuses_retry_and_request_count_semantics() -> None:
    sleeps: list[float] = []
    transport = MacroHttpTransport(
        client=_response_sequence(
            iter([httpx.Response(503), httpx.Response(200, json={"ok": True})])
        ),
        max_attempts=2,
        backoff_seconds=(1,),
        sleep=sleeps.append,
    )

    response = transport.post_json("https://example.test/public", {"query": "safe"})

    assert response.payload == {"ok": True}
    assert response.request_count == 2
    assert sleeps == [1.0]


def test_transport_get_bytes_reuses_retry_and_request_count_semantics() -> None:
    sleeps: list[float] = []
    transport = MacroHttpTransport(
        client=_response_sequence(
            iter([httpx.Response(429), httpx.Response(200, content=b"public")])
        ),
        max_attempts=2,
        backoff_seconds=(1,),
        sleep=sleeps.append,
    )

    response = transport.get_bytes("https://example.test/public.zip")

    assert response.content == b"public"
    assert response.request_count == 2
    assert sleeps == [1.0]


def test_transport_redacts_secret_query_and_header_from_error() -> None:
    secret_query = "query-secret"
    secret_header = "header-secret"
    transport = MacroHttpTransport(
        client=_response_sequence(iter([httpx.Response(503)])),
        max_attempts=1,
        backoff_seconds=(1,),
        sleep=lambda _: None,
    )

    with pytest.raises(ProviderTransientError) as exc_info:
        transport.get_json(
            "https://example.test/data",
            params={"api_key": secret_query},
            headers={"Authorization": f"Bearer {secret_header}"},
        )

    assert secret_query not in str(exc_info.value)
    assert secret_header not in str(exc_info.value)


def test_transport_exhausted_request_error_is_detached_and_counts_attempts() -> None:
    secret = "fred-api-key-for-chain-test"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("network unavailable", request=request)

    transport = MacroHttpTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=2,
        backoff_seconds=(1,),
        sleep=lambda _: None,
    )

    with pytest.raises(ProviderTransientError) as exc_info:
        transport.get_json(
            "https://example.test/data",
            params={"api_key": secret},
            headers={"Authorization": f"Bearer {secret}"},
        )

    error = exc_info.value
    assert error.request_count == 2
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in str(error)
    traceback = error.__traceback__
    while traceback is not None:
        if "app\\connectors\\macro" in traceback.tb_frame.f_code.co_filename:
            assert secret not in repr(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next


def test_transport_rejects_requests_after_close() -> None:
    transport = MacroHttpTransport(
        client=_response_sequence(iter([httpx.Response(200, json={})])),
        max_attempts=1,
        backoff_seconds=(1,),
        sleep=lambda _: None,
    )
    transport.close()

    with pytest.raises(RuntimeError, match="closed"):
        transport.get_json("https://example.test/data")


@pytest.mark.parametrize("status_code", (200, 503))
def test_httpx_request_logs_redact_credentials_without_changing_wire_request(
    status_code: int, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 真实 httpx 在 INFO 输出 request.url；FRED 的 query credential 不能进日志。
    secret = "task11-log-secret-canary"
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)
    caplog.set_level(logging.INFO, logger="httpx")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == secret
        assert request.headers["Authorization"] == f"Bearer {secret}"
        return httpx.Response(status_code, json={"ok": True})

    with MacroHttpTransport(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_attempts=1, backoff_seconds=(0,),
    ) as transport:
        if status_code == 200:
            assert transport.get_json(
                "https://example.test/data", params={"api_key": secret},
                headers={"Authorization": f"Bearer {secret}"},
            ).payload == {"ok": True}
        else:
            with pytest.raises(ProviderTransientError):
                transport.get_json(
                    "https://example.test/data", params={"api_key": secret},
                    headers={"Authorization": f"Bearer {secret}"},
                )
    assert "HTTP Request: GET" in caplog.text
    assert "example.test/data" in caplog.text
    assert secret not in caplog.text


def test_macro_log_redaction_is_scoped_and_survives_another_transport_closing(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)
    caplog.set_level(logging.INFO, logger="httpx")
    macro_secret = "macro-only-canary"
    other_secret = "unrelated-http-canary"
    with MacroHttpTransport(
        client=_response_sequence(iter([httpx.Response(200, json={})])),
        max_attempts=1, backoff_seconds=(0,),
    ) as first, MacroHttpTransport(
        client=_response_sequence(iter([httpx.Response(200, json={})])),
        max_attempts=1, backoff_seconds=(0,),
    ) as second:
        first.close()
        # 证券/其他客户端不经过 MacroHttpTransport，必须维持其日志契约。
        def other_request() -> int:
            with _response_sequence(iter([httpx.Response(200, json={})])) as client:
                return client.get("https://securities.test/data", params={"api_key": other_secret}).status_code

        assert other_request() == 200
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(other_request).result(timeout=5) == 200
        assert second.get_json("https://macro.test/data", params={"api_key": macro_secret}).payload == {}
    assert other_secret in caplog.text
    assert macro_secret not in caplog.text


def test_transport_close_failure_preserves_shared_log_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger("httpx")
    client = _response_sequence(iter([]))
    transport = MacroHttpTransport(client=client, max_attempts=1, backoff_seconds=(0,))
    installed_filters = tuple(logger.filters)
    real_close = client.close

    def failed_close() -> None:
        real_close()
        raise RuntimeError("controlled close failure")

    monkeypatch.setattr(client, "close", failed_close)
    with pytest.raises(RuntimeError, match="controlled close failure"):
        transport.close()
    transport.close()
    assert tuple(logger.filters) == installed_filters
    with pytest.raises(RuntimeError, match="closed"):
        transport.get_json("https://macro.test/data")


@pytest.mark.parametrize("method", ("get_text", "get_bytes", "post_json"))
def test_all_macro_http_methods_redact_logs_and_reset_after_request_error(
    method: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(logging.getLogger("httpx"), "disabled", False)
    caplog.set_level(logging.INFO, logger="httpx")
    secret = "all-methods-canary"
    responses = iter([httpx.Response(200, json={"ok": True}), httpx.Response(503)])
    with MacroHttpTransport(
        client=_response_sequence(responses), max_attempts=1, backoff_seconds=(0,),
    ) as transport:
        operation = getattr(transport, method)
        args = (f"https://macro.test/data?api_key={secret}", {"query": "public"}) if method == "post_json" else (
            "https://macro.test/data",
        )
        kwargs = {} if method == "post_json" else {"params": {"api_key": secret}}
        operation(*args, **kwargs)
        with pytest.raises(ProviderTransientError):
            operation(*args, **kwargs)
        assert secret not in caplog.text
        with _response_sequence(iter([httpx.Response(200, json={})])) as client:
            client.get("https://securities.test/data", params={"api_key": "unchanged-canary"})
        assert "unchanged-canary" in caplog.text


@pytest.mark.parametrize("status_code", (200, 503))
def test_concurrent_close_during_httpx_filter_iteration_keeps_credentials_redacted(
    status_code: int, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 旧列表为 [first, gate, second]；gate暂停时close(first)令迭代跳过second。
    logger = logging.getLogger("httpx")
    monkeypatch.setattr(logger, "disabled", False)
    caplog.set_level(logging.INFO, logger="httpx")
    paused, resume = Event(), Event()
    secret = "concurrent-filter-removal-canary"

    class IterationGate(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            if "api.stlouisfed.org/fred/series/observations" in record.getMessage():
                paused.set()
                assert resume.wait(timeout=5), "filter iteration was not resumed"
            return True

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == secret
        assert request.headers["Authorization"] == f"Bearer {secret}"
        return httpx.Response(status_code, json={"observations": []})

    def request(second: MacroHttpTransport) -> None:
        if status_code == 503:
            with pytest.raises(ProviderTransientError):
                second.get_json(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={"api_key": secret}, headers={"Authorization": f"Bearer {secret}"},
                )
        else:
            assert second.get_json(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"api_key": secret}, headers={"Authorization": f"Bearer {secret}"},
            ).payload == {"observations": []}

    first = MacroHttpTransport(client=_response_sequence(iter([])), max_attempts=1, backoff_seconds=(0,))
    gate = IterationGate()
    logger.addFilter(gate)
    try:
        with MacroHttpTransport(
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_attempts=1, backoff_seconds=(0,),
        ) as second, ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(request, second)
            try:
                assert paused.wait(timeout=5), "HTTPX did not reach the iteration gate"
                first.close()
            finally:
                resume.set()
            future.result(timeout=5)
    finally:
        resume.set()
        first.close()
        logger.removeFilter(gate)
    assert "HTTP Request: GET https://api.stlouisfed.org" in caplog.text
    assert secret not in caplog.text
    assert "api_key=***" in caplog.text


def test_concurrent_transport_lifecycle_keeps_one_shared_filter_and_context_isolated(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = logging.getLogger("httpx")
    monkeypatch.setattr(logger, "disabled", False)
    # 模拟进程首次安装；只在启动worker前隔离测试logger配置，退出时恢复。
    monkeypatch.setattr(logger, "filters", [])
    caplog.set_level(logging.INFO, logger="httpx")
    start, installed = Barrier(8), Barrier(8)

    def lifecycle(index: int) -> None:
        secret = f"parallel-macro-canary-{index}"

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["api_key"] == secret
            return httpx.Response(200, json={"ok": True})

        start.wait(timeout=5)
        with MacroHttpTransport(
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_attempts=1, backoff_seconds=(0,),
        ) as transport:
            installed.wait(timeout=5)
            assert transport.get_json("https://macro.test/data", params={"api_key": secret}).payload == {"ok": True}
        with _response_sequence(iter([httpx.Response(200, json={})])) as other:
            assert other.get("https://securities.test/data", params={"api_key": f"other-client-{index}"}).status_code == 200

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(lifecycle, index) for index in range(8)]
        for future in futures:
            future.result(timeout=10)
    # 安装和关闭不会累积/删除脱敏filter；各线程request结束后不改其他client日志。
    assert len(logger.filters) == 1
    assert "HTTP Request: GET https://macro.test/data" in caplog.text
    assert "parallel-macro-canary-" not in caplog.text
    assert all(f"other-client-{index}" in caplog.text for index in range(8))
