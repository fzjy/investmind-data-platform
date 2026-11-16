from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.macro.contracts import ProviderError
from app.connectors.macro.sanitization import sanitize_macro_text
from app.db.models import MacroSeriesState


@dataclass(frozen=True, slots=True)
class MacroSeriesStateView:
    series_id: str
    catalog_version: str
    last_checked_at: datetime | None
    last_success_at: datetime | None
    latest_observed_at: date | None
    latest_released_at: datetime | None
    active_source: str | None
    fallback_used: bool
    stale: bool
    stale_reason: str | None
    consecutive_failures: int
    last_error_code: str | None = None


class MacroSeriesStateRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        catalog_version: str,
        *,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        if not catalog_version.strip():
            raise ValueError("catalog_version must not be empty")
        self._session_factory = session_factory
        self._catalog_version = catalog_version
        self._redact = (
            sanitize_macro_text
            if redact is None
            else lambda message: redact(sanitize_macro_text(message))
        )

    def mark_success(
        self,
        series_id: str,
        *,
        checked_at: datetime,
        active_source: str,
        latest_observed_at: date | None,
        latest_released_at: datetime | None,
        fallback_used: bool,
        history_incomplete: bool = False,
    ) -> None:
        checked_at = _require_aware(checked_at, "checked_at")
        if latest_released_at is not None:
            latest_released_at = _require_aware(
                latest_released_at,
                "latest_released_at",
            )
        if not series_id.strip():
            raise ValueError("series_id must not be empty")
        if not active_source.strip():
            raise ValueError("active_source must not be empty")

        with self._session_factory.begin() as session:
            state = _get_or_create(
                session,
                series_id,
                self._catalog_version,
            )
            if _newer_state_exists(state, checked_at):
                return
            state.catalog_version = self._catalog_version
            state.last_checked_at = checked_at
            state.last_success_at = checked_at
            if latest_observed_at is not None:
                state.latest_observed_at = latest_observed_at
            if latest_released_at is not None:
                state.latest_released_at = latest_released_at
            state.active_source = active_source
            state.fallback_used = fallback_used
            state.stale = history_incomplete
            state.stale_reason = 'MACRO_HISTORY_GAP' if history_incomplete else None
            state.last_error_code = None
            state.last_error_message = None
            state.consecutive_failures = 0
            state.updated_at = checked_at
            from app.acquisition.adapters.macro import write_series_result
            write_series_result(session, state, status='success', checked=checked_at)

    def mark_stale(
        self,
        series_id: str,
        *,
        checked_at: datetime,
        error: ProviderError,
    ) -> None:
        checked_at = _require_aware(checked_at, "checked_at")
        if not series_id.strip():
            raise ValueError("series_id must not be empty")
        safe_message = self._redact(str(error))

        with self._session_factory.begin() as session:
            state = _get_or_create(
                session,
                series_id,
                self._catalog_version,
            )
            if _newer_state_exists(state, checked_at):
                return
            state.catalog_version = self._catalog_version
            state.last_checked_at = checked_at
            state.stale = True
            state.stale_reason = (
                error.code if state.active_source is not None else "missing"
            )
            state.last_error_code = error.code
            state.last_error_message = safe_message
            state.consecutive_failures += 1
            state.updated_at = checked_at
            from app.acquisition.adapters.macro import write_series_result
            write_series_result(session, state, status='failed', checked=checked_at)

    def get_all(self) -> tuple[MacroSeriesStateView, ...]:
        with self._session_factory() as session:
            states = session.scalars(
                select(MacroSeriesState).order_by(MacroSeriesState.series_id)
            ).all()
        return tuple(_to_view(state) for state in states)


def _get_or_create(
    session: Session,
    series_id: str,
    catalog_version: str,
) -> MacroSeriesState:
    state = session.get(MacroSeriesState, series_id)
    if state is None:
        state = MacroSeriesState(
            series_id=series_id,
            catalog_version=catalog_version,
            fallback_used=False,
            stale=False,
            consecutive_failures=0,
        )
        session.add(state)
    return state


def _newer_state_exists(state: MacroSeriesState, checked_at: datetime) -> bool:
    return any(
        value is not None and _as_utc(value) > checked_at
        for value in (state.last_checked_at, state.last_success_at)
    )


def _to_view(state: MacroSeriesState) -> MacroSeriesStateView:
    return MacroSeriesStateView(
        series_id=state.series_id,
        catalog_version=state.catalog_version,
        last_checked_at=_as_utc(state.last_checked_at),
        last_success_at=_as_utc(state.last_success_at),
        latest_observed_at=state.latest_observed_at,
        latest_released_at=_as_utc(state.latest_released_at),
        active_source=state.active_source,
        fallback_used=state.fallback_used,
        stale=state.stale,
        stale_reason=state.stale_reason,
        consecutive_failures=state.consecutive_failures,
        last_error_code=state.last_error_code,
    )


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
