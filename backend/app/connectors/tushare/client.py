from __future__ import annotations

import logging
import base64
from hashlib import sha256
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import httpx

from app.connectors.tushare.contracts import Scalar, TushareResponse, request_hash
from app.connectors.tushare.errors import (
    TushareAuthError,
    TushareNetworkError,
    TusharePermissionError,
    TushareRateLimitError,
    TushareResponseError,
    TushareSchemaError,
    TushareServerError,
    TushareTransientError,
)
from app.connectors.tushare.rate_limit import RequestRateLimiter


LOGGER = logging.getLogger(__name__)
DEFAULT_ENDPOINT = "https://api.tushare.pro"


class TushareClient:
    def __init__(
        self,
        token: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.2,
        rate_limiter: RequestRateLimiter | None = None,
        before_request: Callable[[], None] | None = None,
        response_observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if not token:
            raise TushareAuthError("Tushare Token 未配置")
        self._token = token
        self._endpoint = endpoint
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._rate_limiter = rate_limiter
        self._before_request = before_request
        self._response_observer = response_observer
        self._client = httpx.Client(transport=transport, timeout=timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def set_before_request(self, callback: Callable[[], None]) -> None:
        """Bind the owning manual job's physical-attempt observer to its dedicated client."""
        self._before_request = callback

    def set_response_observer(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """让拥有者在HTTP状态判断/解码前持久保存响应；不包含请求Token。"""
        self._response_observer = callback

    def __enter__(self) -> "TushareClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def call(
        self,
        api_name: str,
        params: Mapping[str, Scalar],
        fields: Sequence[str],
    ) -> TushareResponse:
        request_id = request_hash(api_name, params, fields)
        payload = {
            "api_name": api_name,
            "token": self._token,
            "params": dict(params),
            "fields": ",".join(fields),
        }
        for attempt in range(self._max_retries + 1):
            try:
                if self._rate_limiter is not None:
                    self._rate_limiter.acquire()
                if self._before_request is not None:
                    self._before_request()
                response = self._client.post(self._endpoint, json=payload)
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise TushareNetworkError("Tushare 请求失败") from exc
                self._sleep(attempt)
                continue

            if self._response_observer is not None:
                original = response.content
                safe = original.replace(self._token.encode('utf-8'), b'[REDACTED]')
                self._response_observer({'statusCode':response.status_code,
                    'contentType':response.headers.get('content-type', '').replace(self._token, '[REDACTED]'),
                    'bodyBase64':base64.b64encode(safe).decode('ascii'), 'bodySha256':sha256(safe).hexdigest(),
                    'byteLength':len(safe), 'credentialsRedacted':safe != original})

            if response.status_code in {401, 403}:
                raise TushareAuthError("Tushare Token 无效或已过期")
            if response.status_code == 429:
                if attempt >= self._max_retries:
                    raise TushareRateLimitError(
                        f"Tushare HTTP {response.status_code}"
                    )
                self._sleep(attempt)
                continue
            if response.status_code >= 500:
                if attempt >= self._max_retries:
                    raise TushareServerError(f"Tushare HTTP {response.status_code}")
                self._sleep(attempt)
                continue
            if response.status_code >= 400:
                raise TushareResponseError(f"Tushare HTTP {response.status_code}")

            result = self._decode(response)
            LOGGER.info(
                "tushare request completed api=%s request_hash=%s rows=%d",
                api_name,
                request_id,
                len(result.items),
            )
            return result
        raise TushareTransientError("Tushare 请求重试耗尽")

    def _sleep(self, attempt: int) -> None:
        delay = self._backoff_seconds * (2**attempt)
        if delay > 0:
            time.sleep(delay)

    def _decode(self, response: httpx.Response) -> TushareResponse:
        try:
            body: Any = response.json()
        except ValueError as exc:
            raise TushareSchemaError("Tushare 响应不是 JSON") from exc
        if not isinstance(body, dict):
            raise TushareSchemaError("Tushare 响应结构不正确")

        code = body.get("code", 0)
        message = str(body.get("msg", "")).replace(self._token, "***")
        if code not in (0, None):
            lowered = message.lower()
            if "token" in lowered or "令牌" in message:
                raise TushareAuthError(message or "Tushare Token 无效")
            rate_limit_markers = (
                "限流",
                "频率",
                "每分钟",
                "每小时",
                "访问次数",
                "rate limit",
                "too many requests",
            )
            if any(marker in lowered for marker in rate_limit_markers):
                raise TushareRateLimitError(message or "Tushare 请求频率受限")
            if "权限" in message or "积分" in message or "permission" in lowered:
                raise TusharePermissionError(message or "Tushare 接口权限不足")
            raise TushareResponseError(message or f"Tushare code={code}")

        data = body.get("data")
        if not isinstance(data, dict):
            raise TushareSchemaError("Tushare 响应缺少 data")
        raw_fields = data.get("fields")
        raw_items = data.get("items")
        if not isinstance(raw_fields, list) or not all(isinstance(item, str) for item in raw_fields):
            raise TushareSchemaError("Tushare fields 结构不正确")
        if not isinstance(raw_items, list) or not all(isinstance(item, list) for item in raw_items):
            raise TushareSchemaError("Tushare items 结构不正确")
        more = data.get("has_more")
        if "has_more" in data and type(more) is not bool:
            raise TushareSchemaError("Tushare has_more 结构不正确")
        result = TushareResponse(
            has_more=more,
            fields=tuple(raw_fields),
            items=tuple(tuple(item) for item in raw_items),
        )
        try:
            result.rows()
        except ValueError as exc:
            raise TushareSchemaError("Tushare fields/items 长度不一致") from exc
        return result
