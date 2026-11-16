from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.macro.contracts import ProviderTransientError
from app.db.models import MacroSeriesState
from app.stocks.jobs.daily_sync_runtime import redact_sensitive_values
from app.macro.jobs.macro_series_state import (
    MacroSeriesStateRepository,
    MacroSeriesStateView,
)


CATALOG_VERSION = "2026-09-20-v1"
CHECKED_AT = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
RELEASED_AT = datetime(2026, 9, 15, 0, 0, tzinfo=UTC)
OBSERVED_AT = date(2026, 8, 1)


@pytest.fixture
def state_fixture(pg_factory) -> Iterator[
    tuple[MacroSeriesStateRepository, sessionmaker[Session]]
]:
    engine, factory = pg_factory.kw["bind"], pg_factory
    repository = MacroSeriesStateRepository(factory, CATALOG_VERSION)
    yield repository, factory
    engine.dispose()


def test_success_clears_errors_and_resets_failure_count(
    state_fixture: tuple[MacroSeriesStateRepository, sessionmaker[Session]],
) -> None:
    repository, factory = state_fixture
    repository.mark_stale(
        "us.cpi.yoy",
        checked_at=CHECKED_AT,
        error=ProviderTransientError("FRED_UNAVAILABLE", "temporary failure"),
    )

    repository.mark_success(
        "us.cpi.yoy",
        checked_at=CHECKED_AT,
        active_source="fred",
        latest_observed_at=OBSERVED_AT,
        latest_released_at=RELEASED_AT,
        fallback_used=False,
    )

    with factory() as session:
        state = session.get(MacroSeriesState, "us.cpi.yoy")
    assert state is not None
    assert state.catalog_version == CATALOG_VERSION
    assert state.last_checked_at == CHECKED_AT
    assert state.last_success_at == CHECKED_AT
    assert state.active_source == "fred"
    assert state.fallback_used is False
    assert state.stale is False
    assert state.stale_reason is None
    assert state.last_error_code is None
    assert state.last_error_message is None
    assert state.consecutive_failures == 0


def test_fallback_success_is_fresh_and_records_actual_source(
    state_fixture: tuple[MacroSeriesStateRepository, sessionmaker[Session]],
) -> None:
    repository, _factory = state_fixture

    repository.mark_success(
        "us.cpi.yoy",
        checked_at=CHECKED_AT,
        active_source="bls",
        latest_observed_at=OBSERVED_AT,
        latest_released_at=RELEASED_AT,
        fallback_used=True,
    )

    state = repository.get_all()[0]
    assert state.active_source == "bls"
    assert state.fallback_used is True
    assert state.stale is False


def test_stale_preserves_active_value_and_increments_failures(
    state_fixture: tuple[MacroSeriesStateRepository, sessionmaker[Session]],
) -> None:
    repository, _factory = state_fixture
    repository.mark_success(
        "us.cpi.yoy",
        checked_at=CHECKED_AT,
        active_source="fred",
        latest_observed_at=OBSERVED_AT,
        latest_released_at=RELEASED_AT,
        fallback_used=False,
    )
    error = ProviderTransientError("FRED_UNAVAILABLE", "temporary failure")

    repository.mark_stale(
        "us.cpi.yoy",
        checked_at=CHECKED_AT.replace(day=21),
        error=error,
    )
    repository.mark_stale(
        "us.cpi.yoy",
        checked_at=CHECKED_AT.replace(day=22),
        error=error,
    )

    state = repository.get_all()[0]
    assert state.active_source == "fred"
    assert state.latest_observed_at == OBSERVED_AT
    assert state.latest_released_at == RELEASED_AT
    assert state.last_success_at == CHECKED_AT
    assert state.fallback_used is False
    assert state.stale is True
    assert state.stale_reason == "FRED_UNAVAILABLE"
    assert state.consecutive_failures == 2


def test_older_replay_updates_cannot_regress_current_series_state(
    state_fixture: tuple[MacroSeriesStateRepository, sessionmaker[Session]],
) -> None:
    repository, _factory = state_fixture
    repository.mark_success(
        "us.cpi.yoy", checked_at=CHECKED_AT, active_source="fred",
        latest_observed_at=OBSERVED_AT, latest_released_at=RELEASED_AT,
        fallback_used=False,
    )
    replay_time = datetime(2019, 9, 20, 9, tzinfo=UTC)
    repository.mark_stale(
        "us.cpi.yoy", checked_at=replay_time,
        error=ProviderTransientError("FRED_UNAVAILABLE", "old replay failure"),
    )
    repository.mark_success(
        "us.cpi.yoy", checked_at=replay_time, active_source="bls",
        latest_observed_at=date(2019, 8, 1),
        latest_released_at=datetime(2019, 9, 19, tzinfo=UTC),
        fallback_used=True,
    )
    state = repository.get_all()[0]
    assert state.last_checked_at == CHECKED_AT
    assert state.last_success_at == CHECKED_AT
    assert state.latest_observed_at == OBSERVED_AT
    assert state.active_source == "fred" and state.stale is False


def test_missing_state_has_no_source_or_value(
    state_fixture: tuple[MacroSeriesStateRepository, sessionmaker[Session]],
) -> None:
    repository, _factory = state_fixture

    repository.mark_stale(
        "cn.cpi.core_yoy",
        checked_at=CHECKED_AT,
        error=ProviderTransientError("NBS_UNAVAILABLE", "no usable source"),
    )

    state = repository.get_all()[0]
    assert state.active_source is None
    assert state.latest_observed_at is None
    assert state.latest_released_at is None
    assert state.last_success_at is None
    assert state.fallback_used is False
    assert state.stale is True
    assert state.stale_reason == "missing"
    assert state.consecutive_failures == 1


def test_every_error_message_passes_through_shared_redactor(pg_factory) -> None:
    engine, factory = pg_factory.kw["bind"], pg_factory
    secret = "provider-secret-value"
    messages: list[str] = []

    def redact(message: str) -> str:
        messages.append(message)
        return redact_sensitive_values(message, secret)

    repository = MacroSeriesStateRepository(
        factory,
        CATALOG_VERSION,
        redact=redact,
    )
    try:
        repository.mark_stale(
            "us.cpi.yoy",
            checked_at=CHECKED_AT,
            error=ProviderTransientError(
                "FRED_UNAVAILABLE",
                f"request failed for configured credential {secret}",
            ),
        )
        repository.mark_stale(
            "cn.cpi.yoy",
            checked_at=CHECKED_AT,
            error=ProviderTransientError(
                "NBS_UNAVAILABLE",
                "postgresql://admin:database-password@localhost/dang",
            ),
        )

        with factory() as session:
            states = session.scalars(
                select(MacroSeriesState).order_by(MacroSeriesState.series_id)
            ).all()
        assert len(messages) == 2
        persisted = " ".join(state.last_error_message or "" for state in states)
        assert secret not in persisted
        assert "database-password" not in persisted
        assert "***" in persisted
    finally:
        engine.dispose()


def test_default_redactor_sanitizes_macro_secret_patterns(pg_factory) -> None:
    engine, factory = pg_factory.kw["bind"], pg_factory
    repository = MacroSeriesStateRepository(factory, CATALOG_VERSION)
    secrets = (
        "token-secret",
        "api-key-secret",
        "authorization-secret",
        "fred-prefix-secret",
        "tushare-prefix-secret",
        "standalone-bearer-secret",
        "quoted-secret",
        "camel-prefix-secret",
        "plain-secret-key",
        "dang-secret-key",
        "camel-secret-key",
        "quoted-secret-key",
        "url-password",
    )
    try:
        repository.mark_stale(
            "us.cpi.yoy",
            checked_at=CHECKED_AT,
            error=ProviderTransientError(
                "FRED_UNAVAILABLE",
                (
                    "token=token-secret "
                    "api_key=api-key-secret "
                    "Authorization: Bearer authorization-secret "
                    "fred_api_key=fred-prefix-secret "
                    '"DANG_TUSHARE_TOKEN"="tushare-prefix-secret" '
                    "retry used Bearer standalone-bearer-secret "
                    '"token" = "quoted-secret" '
                    "fredApiKey=camel-prefix-secret "
                    "secret_key=plain-secret-key "
                    "DANG_FRED_SECRET_KEY=dang-secret-key "
                    "fredSecretKey=camel-secret-key "
                    '"SecretKey" = "quoted-secret-key" '
                    "token_count=4 secretariat=public-office api_key_expiry=2026-10-01 "
                    "https://reader:url-password@example.test/data"
                ),
            ),
        )

        with factory() as session:
            message = session.scalar(
                select(MacroSeriesState.last_error_message)
            )
        assert message is not None
        assert all(secret not in message for secret in secrets)
        assert "token_count=4" in message
        assert "secretariat=public-office" in message
        assert "api_key_expiry=2026-10-01" in message
        assert message.count("***") >= 9
    finally:
        engine.dispose()


def test_get_all_returns_stable_sorted_views(
    state_fixture: tuple[MacroSeriesStateRepository, sessionmaker[Session]],
) -> None:
    repository, _factory = state_fixture
    for series_id in ("us.cpi.yoy", "cn.cpi.yoy"):
        repository.mark_success(
            series_id,
            checked_at=CHECKED_AT,
            active_source="fred" if series_id.startswith("us.") else "nbs",
            latest_observed_at=OBSERVED_AT,
            latest_released_at=RELEASED_AT,
            fallback_used=False,
        )

    states = repository.get_all()

    assert all(isinstance(state, MacroSeriesStateView) for state in states)
    assert [state.series_id for state in states] == ["cn.cpi.yoy", "us.cpi.yoy"]
    assert states[0].last_checked_at == CHECKED_AT
    assert states[0].latest_released_at == RELEASED_AT
