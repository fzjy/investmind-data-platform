from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.macro.contracts import ProviderFetchResult, ProviderObservation
from app.db.models import IngestionBatch, MacroObservation, RejectedRecord, SourcePayload
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSeriesDefinition


CAPTURED_AT = datetime(2026, 9, 20, 8, 30, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture
def repository_fixture(pg_factory) -> Iterator[
    tuple[MacroObservationRepository, sessionmaker[Session], Engine]
]:
    engine, factory = pg_factory.kw["bind"], pg_factory
    yield MacroObservationRepository(factory), factory, engine
    engine.dispose()


@pytest.fixture
def series() -> MacroSeriesDefinition:
    return MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("us.cpi.yoy")


def _observation(
    *,
    provider: str = "fred",
    value: str = "3.1",
    released_at: datetime | None = None,
    revision: str | None = None,
    observed_at: date = date(2026, 8, 1),
) -> ProviderObservation:
    return ProviderObservation(
        provider=provider,
        provider_series_id="CPIAUCSL" if provider == "fred" else "CUUR0000SA0",
        observed_at=observed_at,
        released_at=released_at,
        revision=revision,
        value=Decimal(value),
        source_url=f"https://example.test/{provider}/cpi",
    )


def _result(
    *observations: ProviderObservation,
    provider: str = "fred",
    raw_payload: dict[str, object] | None = None,
) -> ProviderFetchResult:
    return ProviderFetchResult(
        provider=provider,
        status="success",
        observations=tuple(observations),
        raw_payload=raw_payload or {"observations": [{"value": "3.1"}]},
        request_count=1,
    )


def test_known_provider_revision_is_idempotent(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, factory, _engine = repository_fixture
    observation = _observation(
        released_at=datetime(2026, 9, 15, tzinfo=UTC),
        revision="provider-v1",
    )

    first = repository.persist(series, _result(observation), CAPTURED_AT)
    same = repository.persist(series, _result(observation), CAPTURED_AT + DAY)

    assert (first.inserted, first.unchanged, first.rejected) == (1, 0, 0)
    assert (same.inserted, same.unchanged, same.rejected) == (0, 1, 0)
    with factory() as session:
        rows = session.scalars(select(MacroObservation)).all()
    assert len(rows) == 1
    assert rows[0].revision == "provider-v1"
    assert rows[0].released_at == datetime(2026, 9, 15, tzinfo=UTC)


@pytest.mark.parametrize("release_failure", [False, True])
def test_nbs_failure_evidence_reaches_raw_audit_without_core_observations(
    repository_fixture, release_failure,
) -> None:
    import httpx
    from hashlib import sha256
    import json
    from app.connectors.macro.contracts import MacroFetchWindow
    from app.connectors.macro.nbs import NbsMacroProvider
    from app.connectors.macro.transport import MacroHttpTransport

    repository, factory, _engine = repository_fixture
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require("cn.cpi.core_yoy")
    index_url = "https://www.stats.gov.cn/sj/zxfb/"
    release_url = index_url + "202609/t20260909_1965263.html"
    title = "2026年8月份居民消费价格同比上涨0.8%"
    failed_body = '<meta name="ArticleTitle" content="api_key=should-not-be-stored">'

    def handle(request: httpx.Request) -> httpx.Response:
        if release_failure and str(request.url) == index_url:
            return httpx.Response(200, text=(
                f'<a href="{release_url}" title="{title}">{title}</a>'
                '<script>createPageHTML(1, 0, "index", "html");</script>'
            ))
        return httpx.Response(200, text=failed_body)

    with MacroHttpTransport(client=httpx.Client(transport=httpx.MockTransport(handle)),
                            max_attempts=1, backoff_seconds=(0,)) as transport:
        window = MacroFetchWindow(date(2026, 8, 1), date(2026, 9, 20), CAPTURED_AT)
        result = NbsMacroProvider(transport).fetch(series, series.sources[0], window)
    batch_id = repository.audit_request(series, series.sources[0], window, result,
                                        CAPTURED_AT, "nbs-diagnostics-controlled-test")
    with factory() as session:
        audit = session.scalar(select(SourcePayload).where(SourcePayload.batch_id == batch_id))
        batch = session.get(IngestionBatch, batch_id)
        assert session.scalar(select(MacroObservation)) is None
    assert batch.status == "failed" and batch.request_count == (2 if release_failure else 1)
    assert audit.payload["status"] == "failed"
    data = audit.payload["data"]
    assert data["errorCode"] == ("NBS_TITLE_MISMATCH" if release_failure else "NBS_INDEX_SCHEMA_INVALID")
    context = data["failureContext"]
    assert context["url"] == (release_url if release_failure else index_url)
    assert context["textSha256"] == sha256(failed_body.encode()).hexdigest()
    assert context["responseReceived"] is True
    assert "should-not-be-stored" not in json.dumps(audit.payload)


@pytest.mark.parametrize("base,current,revised,stored", [
    ("100", "103.1", "103.4", "3.1000000000"),
    ("300", "310", "311", "3.3333333333"),
])
def test_fred_real_date_vintages_persist_and_replay_without_end_boundary_conflict(
    repository_fixture, series, base, current, revised, stored,
) -> None:
    from app.connectors.macro.fred import FredMacroProvider
    from app.connectors.macro.contracts import MacroFetchWindow
    from app.connectors.macro.transport import MacroHttpTransport
    import httpx

    repository, factory, _engine = repository_fixture
    revisions = [
        {"date": "2025-08-01", "value": base, "realtime_start": "2026-09-15", "realtime_end": "9999-12-31"},
        {"date": "2026-08-01", "value": current, "realtime_start": "2026-09-15", "realtime_end": "2026-09-19"},
        {"date": "2026-08-01", "value": revised, "realtime_start": "2026-09-20", "realtime_end": "9999-12-31"},
    ]
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("vintagedates"):
            return httpx.Response(200, json={"vintage_dates": ["2026-09-15", "2026-09-20"], "count": 2})
        return httpx.Response(200, json={"observations": revisions, "count": 3})
    transport = MacroHttpTransport(client=httpx.Client(transport=httpx.MockTransport(handle)), max_attempts=1, backoff_seconds=(0,))
    provider = FredMacroProvider("test-only-key", transport=transport)
    window = MacroFetchWindow(date(2026, 8, 1), date(2026, 9, 20), CAPTURED_AT)
    result = provider.fetch(series, series.sources[0], window)
    first = repository.persist(series, result, CAPTURED_AT)
    repeated = repository.persist(series, result, CAPTURED_AT + DAY)
    assert (first.inserted, first.rejected, repeated.unchanged, repeated.rejected) == (2, 0, 2, 0)
    assert repository.latest_visible(series, datetime(2026, 9, 16, tzinfo=UTC), window.end)[2] == datetime(2026, 9, 15, tzinfo=UTC)
    with factory() as session:
        assert set(session.scalars(select(MacroObservation.revision))) == {"2026-09-15", "2026-09-20"}
        assert session.scalar(select(MacroObservation.value).where(
            MacroObservation.revision == "2026-09-15")) == Decimal(stored)


def test_reused_provider_revision_with_changed_value_is_rejected(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, factory, _engine = repository_fixture
    released_at = datetime(2026, 9, 15, tzinfo=UTC)
    repository.persist(
        series,
        _result(_observation(value="3.1", released_at=released_at, revision="provider-v1")),
        CAPTURED_AT,
    )

    result = repository.persist(
        series,
        _result(_observation(value="3.2", released_at=released_at, revision="provider-v1")),
        CAPTURED_AT + DAY,
    )

    assert (result.inserted, result.unchanged, result.rejected) == (0, 0, 1)
    with factory() as session:
        assert session.scalar(select(MacroObservation.value)) == Decimal("3.1000000000")
        rejected = session.scalar(select(RejectedRecord))
    assert rejected is not None
    assert rejected.reason == "PROVIDER_REVISION_VALUE_CONFLICT"


def test_unknown_release_date_is_stable_until_value_changes(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, factory, _engine = repository_fixture

    first = repository.persist(series, _result(_observation(value="3.1")), CAPTURED_AT)
    same = repository.persist(
        series,
        _result(_observation(value="3.1")),
        CAPTURED_AT + DAY,
    )
    changed = repository.persist(
        series,
        _result(_observation(value="3.2")),
        CAPTURED_AT + 2 * DAY,
    )

    assert (first.inserted, same.inserted, changed.inserted) == (1, 0, 1)
    assert same.unchanged == 1
    with factory() as session:
        rows = session.scalars(
            select(MacroObservation).order_by(MacroObservation.released_at)
        ).all()
    assert len(rows) == 2
    assert rows[0].revision.startswith("initial-import:")
    assert rows[0].released_at == CAPTURED_AT
    assert rows[1].revision.startswith("detected:")
    assert rows[1].released_at == (CAPTURED_AT + 2 * DAY)


def test_same_observation_from_different_sources_is_preserved(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, factory, _engine = repository_fixture
    released_at = datetime(2026, 9, 15, tzinfo=UTC)

    fred = repository.persist(
        series,
        _result(_observation(released_at=released_at, revision="fred-v1")),
        CAPTURED_AT,
    )
    bls = repository.persist(
        series,
        _result(
            _observation(
                provider="bls",
                released_at=released_at,
                revision="bls-v1",
            ),
            provider="bls",
        ),
        CAPTURED_AT,
    )

    assert fred.inserted == bls.inserted == 1
    with factory() as session:
        assert set(session.scalars(select(MacroObservation.source))) == {"fred", "bls"}


def test_latest_visible_excludes_future_release_and_observation(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, _factory, _engine = repository_fixture
    repository.persist(series, _result(_observation(
        observed_at=date(2019, 9, 18),
        released_at=datetime(2019, 9, 19, tzinfo=UTC), revision="fred-old",
    )), CAPTURED_AT)
    repository.persist(series, _result(_observation(
        provider="bls", observed_at=date(2019, 9, 18),
        released_at=datetime(2019, 9, 19, tzinfo=UTC), revision="bls-old",
    ), provider="bls"), CAPTURED_AT)
    repository.persist(series, _result(_observation(
        observed_at=date(2019, 9, 18),
        released_at=datetime(2026, 9, 19, tzinfo=UTC), revision="fred-future-vintage",
    )), CAPTURED_AT)
    repository.persist(series, _result(_observation(
        observed_at=date(2026, 9, 18),
        released_at=datetime(2019, 9, 19, tzinfo=UTC), revision="future-period",
    )), CAPTURED_AT)
    assert repository.latest_visible(
        series, datetime(2019, 9, 20, 9, tzinfo=UTC), date(2019, 9, 20),
    ) == ("fred", date(2019, 9, 18), datetime(2019, 9, 19, tzinfo=UTC))


def test_unknown_release_uses_actual_capture_not_historical_as_of(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, factory, _engine = repository_fixture
    captured_at = datetime(2026, 9, 23, 10, tzinfo=UTC)
    repository.persist(series, _result(_observation(
        observed_at=date(2019, 9, 18), released_at=None, revision=None,
    )), captured_at)
    with factory() as session:
        row = session.scalar(select(MacroObservation).where(MacroObservation.series_id == series.id))
    assert row is not None
    assert row.released_at == captured_at
    assert row.revision.startswith("initial-import:")
    assert repository.latest_visible(
        series, datetime(2019, 9, 20, 9, tzinfo=UTC), date(2019, 9, 20),
    ) is None


def test_json_payload_is_saved_as_a_json_audit_envelope(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, factory, _engine = repository_fixture
    raw = {"observations": [{"date": "2026-08-01", "value": "3.1"}]}

    persisted = repository.persist(
        series,
        _result(_observation(), raw_payload=raw),
        CAPTURED_AT,
    )

    with factory() as session:
        payload = session.scalar(select(SourcePayload))
        batch = session.get(IngestionBatch, persisted.batch_id)
    assert payload is not None
    assert payload.payload == {
        "format": "json",
        "provider": "fred",
        "seriesId": "us.cpi.yoy",
        "status": "success",
        "requestCount": 1,
        "data": raw,
    }
    assert payload.captured_at == CAPTURED_AT
    assert batch is not None
    assert (batch.source, batch.dataset, batch.status) == (
        "fred",
        "macro:us.cpi.yoy",
        "completed",
    )


@pytest.mark.parametrize("file_type", ["csv", "xlsx"])
def test_file_payload_is_saved_as_a_binary_free_audit_envelope(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
    file_type: str,
) -> None:
    repository, factory, _engine = repository_fixture
    raw = {
        "fileType": file_type,
        "contentSha256": "a" * 64,
        "sourceUrl": "https://example.test/download",
        "metadata": {"sheet": "Data"},
        "rows": [{"period": "2026-08", "value": "3.1"}],
    }

    repository.persist(
        series,
        _result(_observation(), raw_payload=raw),
        CAPTURED_AT,
    )

    with factory() as session:
        audit = session.scalar(select(SourcePayload.payload))
    assert audit == {
        "format": "file",
        "fileType": file_type,
        "contentSha256": "a" * 64,
        "provider": "fred",
        "seriesId": "us.cpi.yoy",
        "status": "success",
        "requestCount": 1,
        "metadata": {
            "sourceUrl": "https://example.test/download",
            "metadata": {"sheet": "Data"},
            "rows": [{"period": "2026-08", "value": "3.1"}],
        },
    }


def test_non_finite_and_inconsistent_observations_are_rejected(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, factory, _engine = repository_fixture
    invalid = (
        _observation(value="NaN"),
        _observation(provider="bls"),
        _observation(released_at=datetime(2026, 9, 15), revision="naive-time"),
    )

    persisted = repository.persist(series, _result(*invalid), CAPTURED_AT)

    assert (persisted.received, persisted.inserted, persisted.rejected) == (3, 0, 3)
    with factory() as session:
        assert session.scalar(select(MacroObservation.id)) is None
        rejected = session.scalars(
            select(RejectedRecord).order_by(RejectedRecord.row_number)
        ).all()
    assert [row.reason for row in rejected] == [
        "NON_FINITE_VALUE",
        "PROVIDER_MISMATCH",
        "RELEASED_AT_TIMEZONE_REQUIRED",
    ]


def test_rejected_record_recursively_sanitizes_every_observation_field(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, factory, _engine = repository_fixture
    secrets = (
        "provider-secret",
        "series-secret",
        "revision-secret",
        "value-secret",
        "provider-secret-key",
        "series-secret-key",
        "revision-secret-key",
        "value-secret-key",
        "url-password",
    )
    invalid = ProviderObservation(
        provider="fred Bearer provider-secret secret_key=provider-secret-key",
        provider_series_id=(
            "CPIAUCSL?fred_api_key=series-secret"
            "&DANG_FRED_SECRET_KEY=series-secret-key"
        ),
        observed_at=date(2026, 8, 1),
        released_at=CAPTURED_AT,
        revision=(
            '"DANG_TUSHARE_TOKEN"="revision-secret" '
            '"SecretKey"="revision-secret-key"'
        ),
        value=cast(Decimal, '"token"="value-secret" "secret_key"="value-secret-key"'),
        source_url="https://reader:url-password@example.test/cpi",
    )

    persisted = repository.persist(series, _result(invalid), CAPTURED_AT)

    assert persisted.rejected == 1
    with factory() as session:
        raw_record = session.scalar(select(RejectedRecord.raw_record))
    assert raw_record is not None
    stored = repr(raw_record)
    assert all(secret not in stored for secret in secrets)
    assert stored.count("***") >= 5


def test_audit_payload_preserves_non_secret_tokenized_keys(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, factory, _engine = repository_fixture
    secrets = (
        "fred-secret",
        "tushare-secret",
        "client-secret",
        "password-secret",
        "authorization-secret",
        "plain-secret-key",
        "dang-secret-key",
        "camel-secret-key",
        "quoted-secret-key",
    )
    repository.persist(
        series,
        _result(
            _observation(),
            raw_payload={
                "token_count": 4,
                "secretariat": "public-office",
                "api_key_expiry": "2026-10-01",
                "fred_api_key": secrets[0],
                "DANG_TUSHARE_TOKEN": secrets[1],
                "client_secret": secrets[2],
                "password": secrets[3],
                "authorization": secrets[4],
                "secret_key": secrets[5],
                "DANG_TUSHARE_SECRET_KEY": secrets[6],
                "fredSecretKey": secrets[7],
                "metadata": {"note": f'"SecretKey"="{secrets[8]}"'},
            },
        ),
        CAPTURED_AT,
    )

    with factory() as session:
        payload = session.scalar(select(SourcePayload.payload))
    assert payload is not None
    data = payload["data"]
    assert isinstance(data, dict)
    assert data == {
        "token_count": 4,
        "secretariat": "public-office",
        "api_key_expiry": "2026-10-01",
        "metadata": {"note": '"SecretKey"="***"'},
    }
    assert all(secret not in repr(payload) for secret in secrets)


def test_request_hash_and_audit_payload_exclude_secrets(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, factory, _engine = repository_fixture
    first_secret = "fred-secret-one"
    second_secret = "fred-secret-two"

    for offset, secret in enumerate((first_secret, second_secret)):
        repository.persist(
            series,
            _result(
                _observation(),
                raw_payload={
                    "apiKey": secret,
                    "url": f"https://example.test/data?api_key={secret}&units=pc1",
                    "headers": {"Authorization": f"Bearer {secret}"},
                    "observations": [],
                },
            ),
            CAPTURED_AT + offset * DAY,
        )

    with factory() as session:
        payloads = session.scalars(
            select(SourcePayload).order_by(SourcePayload.id)
        ).all()
    assert len(payloads) == 2
    assert payloads[0].request_hash == payloads[1].request_hash
    stored = repr([payload.payload for payload in payloads])
    assert first_secret not in stored
    assert second_secret not in stored
    assert "apiKey" not in stored


def test_core_failure_rolls_back_raw_and_core_in_one_transaction(
    repository_fixture: tuple[MacroObservationRepository, sessionmaker[Session], Engine],
    series: MacroSeriesDefinition,
) -> None:
    repository, factory, engine = repository_fixture

    def fail_core_insert(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if "INSERT" in statement and "macro_observation" in statement:
            raise RuntimeError("forced core failure")

    event.listen(engine, "before_cursor_execute", fail_core_insert)
    try:
        with pytest.raises(RuntimeError, match="forced core failure"):
            repository.persist(series, _result(_observation()), CAPTURED_AT)
    finally:
        event.remove(engine, "before_cursor_execute", fail_core_insert)

    with factory() as session:
        assert session.scalar(select(IngestionBatch.id)) is None
        assert session.scalar(select(SourcePayload.id)) is None
        assert session.scalar(select(RejectedRecord.id)) is None
        assert session.scalar(select(MacroObservation.id)) is None
