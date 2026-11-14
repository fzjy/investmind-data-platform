from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol

from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


ProviderStatus = Literal["success", "no_change", "unavailable", "failed"]


class ProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        retryable: bool,
        request_count: int = 0,
    ) -> None:
        if request_count < 0:
            raise ValueError("request_count must not be negative")
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.request_count = request_count
        super().__init__(self.message)

    def sanitized_copy(self, *, request_count: int | None = None) -> "ProviderError":
        safe_request_count = self.request_count if request_count is None else request_count
        if type(self) is ProviderError:
            return ProviderError(
                self.code,
                self.message,
                retryable=self.retryable,
                request_count=safe_request_count,
            )
        return type(self)(
            self.code,
            self.message,
            request_count=safe_request_count,
        )


class ProviderAuthError(ProviderError):
    def __init__(
        self,
        code: str = "PROVIDER_AUTH_FAILED",
        message: str | None = None,
        *,
        request_count: int = 0,
    ) -> None:
        super().__init__(code, message, retryable=False, request_count=request_count)


class ProviderPermissionError(ProviderError):
    def __init__(
        self,
        code: str = "PROVIDER_PERMISSION_DENIED",
        message: str | None = None,
        *,
        request_count: int = 0,
    ) -> None:
        super().__init__(code, message, retryable=False, request_count=request_count)


class ProviderRateLimitError(ProviderError):
    def __init__(
        self,
        code: str = "PROVIDER_RATE_LIMITED",
        message: str | None = None,
        *,
        request_count: int = 0,
    ) -> None:
        super().__init__(code, message, retryable=True, request_count=request_count)


class ProviderTransientError(ProviderError):
    def __init__(
        self,
        code: str = "PROVIDER_TRANSIENT_FAILURE",
        message: str | None = None,
        *,
        request_count: int = 0,
    ) -> None:
        super().__init__(code, message, retryable=True, request_count=request_count)


class ProviderSchemaError(ProviderError):
    def __init__(
        self,
        code: str = "PROVIDER_SCHEMA_INVALID",
        message: str | None = None,
        *,
        request_count: int = 0,
    ) -> None:
        super().__init__(code, message, retryable=False, request_count=request_count)


@dataclass(frozen=True, slots=True)
class MacroFetchWindow:
    start: date
    end: date
    as_of: datetime


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    provider: str
    provider_series_id: str
    observed_at: date
    released_at: datetime | None
    revision: str | None
    value: Decimal
    source_url: str
    # Exclusive UTC boundary; None means no known invalidation.
    valid_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProviderFetchResult:
    provider: str
    status: ProviderStatus
    observations: tuple[ProviderObservation, ...]
    raw_payload: dict[str, object]
    request_count: int
    error: ProviderError | None = None


class MacroProvider(Protocol):
    provider_id: str

    def fetch(
        self,
        series: MacroSeriesDefinition,
        source: MacroSourceDefinition,
        window: MacroFetchWindow,
    ) -> ProviderFetchResult: ...


class UnavailableMacroProvider:
    def __init__(self, provider_id: str, error: ProviderError) -> None:
        self.provider_id = provider_id
        self._error = error

    def fetch(
        self,
        series: MacroSeriesDefinition,
        source: MacroSourceDefinition,
        window: MacroFetchWindow,
    ) -> ProviderFetchResult:
        return ProviderFetchResult(
            provider=self.provider_id,
            status="unavailable",
            observations=(),
            raw_payload={},
            request_count=0,
            error=self._error,
        )
