from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from threading import Lock

import httpx

from app.connectors.macro.contracts import (
    ProviderAuthError,
    ProviderError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderSchemaError,
    ProviderTransientError,
)
from app.connectors.macro.sanitization import sanitize_macro_text


class _CredentialLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # HTTPX 的 INFO request.url 包含 FRED query key；保留诊断但先脱敏。
        active_url = _ACTIVE_MACRO_URL.get()
        logged_url = next((arg for arg in record.args if isinstance(arg, httpx.URL)), None) if isinstance(record.args, tuple) else None
        # HTTPX response hooks can issue unrelated requests inside this context.
        # Keep their logs unchanged, but still redact redirects carrying our credentials.
        own_request = logged_url is None or active_url is None or logged_url == active_url
        if logged_url is not None and active_url is not None:
            own_request = own_request or any(
                value and logged_url.params.get(key) == value
                for key, value in active_url.params.multi_items()
                if key.casefold() in {"api_key", "apikey", "token", "access_token"}
            )
        if _ACTIVE_MACRO_FILTER.get() is self and own_request:
            secrets = tuple(value for key, value in active_url.params.multi_items()
                if key.casefold() in {"api_key", "apikey", "token", "access_token"}) if active_url is not None else ()
            record.msg = sanitize_macro_text(record.getMessage(), *secrets)
            record.args = ()
        return True

    @contextmanager
    def request_context(self, url: httpx.URL) -> Iterator[None]:
        token = _ACTIVE_MACRO_FILTER.set(self)
        url_token = _ACTIVE_MACRO_URL.set(url)
        try:
            yield
        finally:
            _ACTIVE_MACRO_FILTER.reset(token)
            _ACTIVE_MACRO_URL.reset(url_token)


_ACTIVE_MACRO_FILTER: ContextVar[_CredentialLogFilter | None] = ContextVar(
    "macro_http_log_filter", default=None,
)
_ACTIVE_MACRO_URL: ContextVar[httpx.URL | None] = ContextVar("macro_http_log_url", default=None)
_SHARED_MACRO_LOG_FILTER = _CredentialLogFilter()
_FILTER_INSTALL_LOCK = Lock()


def _install_credential_log_filter() -> _CredentialLogFilter:
    # Filterer直接遍历可变列表：实例close不能remove导致别的请求跳过脱敏。
    # 同一filter进程级常驻；加锁保证首次并发安装去重，非macro上下文不改日志。
    with _FILTER_INSTALL_LOCK:
        # Logger过滤器不沿propagation继承；必须覆盖实际发响应头事件的子logger。
        for name in ("httpx", "httpcore", "httpcore.connection", "httpcore.http11",
            "httpcore.http2", "httpcore.proxy", "httpcore.socks"):
            logging.getLogger(name).addFilter(_SHARED_MACRO_LOG_FILTER)
    return _SHARED_MACRO_LOG_FILTER


@dataclass(frozen=True, slots=True)
class JsonTransportResponse:
    payload: dict[str, object]
    request_count: int


@dataclass(frozen=True, slots=True)
class TextTransportResponse:
    text: str
    request_count: int


@dataclass(frozen=True, slots=True)
class BinaryTransportResponse:
    content: bytes
    request_count: int


class MacroHttpTransport:
    def __init__(
        self,
        *,
        client: httpx.Client,
        max_attempts: int,
        backoff_seconds: Sequence[float],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if not backoff_seconds:
            raise ValueError("backoff_seconds must not be empty")
        self._client = client
        self._max_attempts = max_attempts
        self._backoff_seconds = tuple(backoff_seconds)
        self._sleep = sleep
        self._closed = False
        self._log_filter = _install_credential_log_filter()

    def close(self) -> None:
        if not self._closed:
            try:
                self._client.close()
            finally:
                self._closed = True

    def __enter__(self) -> "MacroHttpTransport":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> JsonTransportResponse:
        if self._closed:
            raise RuntimeError("macro HTTP transport is closed")

        pending_error: ProviderError | None = None
        response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            try:
                with self._log_filter.request_context(self._client.build_request("GET", url, params=params).url):
                    response = self._client.get(url, params=params, headers=headers)
            except httpx.RequestError:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderTransientError(
                        "HTTP_REQUEST_FAILED",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._backoff(attempt))
                continue

            if response.status_code == 401:
                pending_error = ProviderAuthError(
                    "HTTP_AUTH_FAILED",
                    request_count=attempt + 1,
                )
            elif response.status_code == 403:
                pending_error = ProviderPermissionError(
                    "HTTP_PERMISSION_DENIED",
                    request_count=attempt + 1,
                )
            elif response.status_code == 429:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderRateLimitError(
                        "HTTP_RATE_LIMITED",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._retry_delay(response, attempt))
                continue
            elif response.status_code >= 500:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderTransientError(
                        "HTTP_SERVER_ERROR",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._backoff(attempt))
                continue
            elif response.status_code >= 400:
                pending_error = ProviderSchemaError(
                    "HTTP_CLIENT_ERROR",
                    request_count=attempt + 1,
                )
            else:
                try:
                    decoded: object = response.json()
                except ValueError:
                    pending_error = ProviderSchemaError(
                        "HTTP_INVALID_JSON",
                        request_count=attempt + 1,
                    )
                else:
                    if not isinstance(decoded, dict):
                        pending_error = ProviderSchemaError(
                            "HTTP_JSON_OBJECT_REQUIRED",
                            request_count=attempt + 1,
                        )
                    else:
                        return JsonTransportResponse(payload=decoded, request_count=attempt + 1)

            if pending_error is not None:
                break

        if pending_error is None:
            pending_error = ProviderTransientError(
                "HTTP_RETRY_EXHAUSTED",
                request_count=self._max_attempts,
            )
        params = None
        headers = None
        response = None
        url = ""
        raise pending_error

    def get_text(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> TextTransportResponse:
        if self._closed:
            raise RuntimeError("macro HTTP transport is closed")

        pending_error: ProviderError | None = None
        response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            try:
                with self._log_filter.request_context(self._client.build_request("GET", url, params=params).url):
                    response = self._client.get(url, params=params, headers=headers)
            except httpx.RequestError:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderTransientError(
                        "HTTP_REQUEST_FAILED",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._backoff(attempt))
                continue

            if response.status_code == 401:
                pending_error = ProviderAuthError(
                    "HTTP_AUTH_FAILED",
                    request_count=attempt + 1,
                )
            elif response.status_code == 403:
                pending_error = ProviderPermissionError(
                    "HTTP_PERMISSION_DENIED",
                    request_count=attempt + 1,
                )
            elif response.status_code == 429:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderRateLimitError(
                        "HTTP_RATE_LIMITED",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._retry_delay(response, attempt))
                continue
            elif response.status_code >= 500:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderTransientError(
                        "HTTP_SERVER_ERROR",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._backoff(attempt))
                continue
            elif response.status_code >= 400:
                pending_error = ProviderSchemaError(
                    "HTTP_CLIENT_ERROR",
                    request_count=attempt + 1,
                )
            else:
                return TextTransportResponse(text=response.text, request_count=attempt + 1)

            if pending_error is not None:
                break

        if pending_error is None:
            pending_error = ProviderTransientError(
                "HTTP_RETRY_EXHAUSTED",
                request_count=self._max_attempts,
            )
        params = None
        headers = None
        response = None
        url = ""
        raise pending_error

    def post_json(
        self,
        url: str,
        payload: Mapping[str, object],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> JsonTransportResponse:
        if self._closed:
            raise RuntimeError("macro HTTP transport is closed")

        pending_error: ProviderError | None = None
        response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            try:
                with self._log_filter.request_context(self._client.build_request("POST", url).url):
                    response = self._client.post(url, json=payload, headers=headers)
            except httpx.RequestError:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderTransientError(
                        "HTTP_REQUEST_FAILED",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._backoff(attempt))
                continue

            if response.status_code == 401:
                pending_error = ProviderAuthError(
                    "HTTP_AUTH_FAILED",
                    request_count=attempt + 1,
                )
            elif response.status_code == 403:
                pending_error = ProviderPermissionError(
                    "HTTP_PERMISSION_DENIED",
                    request_count=attempt + 1,
                )
            elif response.status_code == 429:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderRateLimitError(
                        "HTTP_RATE_LIMITED",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._retry_delay(response, attempt))
                continue
            elif response.status_code >= 500:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderTransientError(
                        "HTTP_SERVER_ERROR",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._backoff(attempt))
                continue
            elif response.status_code >= 400:
                pending_error = ProviderSchemaError(
                    "HTTP_CLIENT_ERROR",
                    request_count=attempt + 1,
                )
            else:
                try:
                    decoded: object = response.json()
                except ValueError:
                    pending_error = ProviderSchemaError(
                        "HTTP_INVALID_JSON",
                        request_count=attempt + 1,
                    )
                else:
                    if not isinstance(decoded, dict):
                        pending_error = ProviderSchemaError(
                            "HTTP_JSON_OBJECT_REQUIRED",
                            request_count=attempt + 1,
                        )
                    else:
                        return JsonTransportResponse(payload=decoded, request_count=attempt + 1)

            if pending_error is not None:
                break

        if pending_error is None:
            pending_error = ProviderTransientError(
                "HTTP_RETRY_EXHAUSTED",
                request_count=self._max_attempts,
            )
        payload = {}
        headers = None
        response = None
        url = ""
        raise pending_error

    def get_bytes(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> BinaryTransportResponse:
        if self._closed:
            raise RuntimeError("macro HTTP transport is closed")

        pending_error: ProviderError | None = None
        response: httpx.Response | None = None
        for attempt in range(self._max_attempts):
            try:
                with self._log_filter.request_context(self._client.build_request("GET", url, params=params).url):
                    response = self._client.get(url, params=params, headers=headers)
            except httpx.RequestError:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderTransientError(
                        "HTTP_REQUEST_FAILED",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._backoff(attempt))
                continue

            if response.status_code == 401:
                pending_error = ProviderAuthError(
                    "HTTP_AUTH_FAILED",
                    request_count=attempt + 1,
                )
            elif response.status_code == 403:
                pending_error = ProviderPermissionError(
                    "HTTP_PERMISSION_DENIED",
                    request_count=attempt + 1,
                )
            elif response.status_code == 429:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderRateLimitError(
                        "HTTP_RATE_LIMITED",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._retry_delay(response, attempt))
                continue
            elif response.status_code >= 500:
                if attempt == self._max_attempts - 1:
                    pending_error = ProviderTransientError(
                        "HTTP_SERVER_ERROR",
                        request_count=attempt + 1,
                    )
                    break
                self._sleep(self._backoff(attempt))
                continue
            elif response.status_code >= 400:
                pending_error = ProviderSchemaError(
                    "HTTP_CLIENT_ERROR",
                    request_count=attempt + 1,
                )
            else:
                return BinaryTransportResponse(
                    content=response.content,
                    request_count=attempt + 1,
                )

            if pending_error is not None:
                break

        if pending_error is None:
            pending_error = ProviderTransientError(
                "HTTP_RETRY_EXHAUSTED",
                request_count=self._max_attempts,
            )
        params = None
        headers = None
        response = None
        url = ""
        raise pending_error

    def _backoff(self, attempt: int) -> float:
        return self._backoff_seconds[min(attempt, len(self._backoff_seconds) - 1)]

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        delay = self._retry_after(response)
        return delay if delay is not None else self._backoff(attempt)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw_value = response.headers.get("Retry-After")
        if raw_value is None:
            return None
        try:
            return min(max(float(raw_value), 0.0), 60.0)
        except ValueError:
            try:
                target = parsedate_to_datetime(raw_value)
                if target.utcoffset() is None:
                    return None
                return min(max((target - datetime.now(UTC)).total_seconds(), 0.0), 60.0)
            except (ValueError, TypeError, OverflowError):
                return None
