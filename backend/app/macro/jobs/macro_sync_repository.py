from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.macro.contracts import MacroFetchWindow, ProviderFetchResult, ProviderObservation, ProviderSchemaError
from app.connectors.macro.sanitization import sanitize_macro_value
from app.db.models import IngestionBatch, MacroObservation, RejectedRecord, SourcePayload
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition
from app.macro.import_lineage import verify_import_lineage
from app.macro.marts.macro import select_visible_observations


_FILE_TYPES = frozenset({"csv", "xlsx"})


@dataclass(frozen=True, slots=True)
class MacroPersistResult:
    batch_id: str
    received: int
    inserted: int
    unchanged: int
    rejected: int
    latest_observed_at: date | None
    latest_released_at: datetime | None


@dataclass(frozen=True, slots=True)
class _PersistDecision:
    status: str
    observed_at: date | None = None
    released_at: datetime | None = None
    reason: str | None = None


class MacroObservationRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def ensure_history_observation(
        self, series: MacroSeriesDefinition, observation: ProviderObservation, as_of: datetime,
        *, require_present: bool = False,
    ) -> None:
        """历史回填专用：不把中断后的冲突当作普通修订，也不以计数代替可见性。

        日常同步的修订规则不变。回填遇已有不同值/发布时间的同源同月数据，
        停在pending供后续审计，不能自动选择哪个版本代表历史报告。
        """
        cutoff = _require_aware_datetime(as_of, 'as_of')
        if observation.released_at is None:
            raise ProviderSchemaError('NBS_HISTORY_CORE_NOT_VISIBLE')
        released = _normalize_datetime(observation.released_at)
        with self._session_factory() as session:
            rows = session.scalars(select(MacroObservation).where(
                MacroObservation.series_id == series.id,
                MacroObservation.source == observation.provider,
                MacroObservation.observed_at == observation.observed_at,
            )).all()
        if not rows:
            if require_present:
                raise ProviderSchemaError('NBS_HISTORY_CORE_NOT_VISIBLE')
            return
        if any(row.value != observation.value or _normalize_datetime(row.released_at) != released for row in rows):
            raise ProviderSchemaError('NBS_HISTORY_CORE_CONFLICT')
        if not any(_normalize_datetime(row.released_at) <= cutoff
                   and (row.valid_until is None or _normalize_datetime(row.valid_until) > cutoff) for row in rows):
            raise ProviderSchemaError('NBS_HISTORY_CORE_NOT_VISIBLE')

    def audit_request(self, series: MacroSeriesDefinition, source: MacroSourceDefinition,
                      window: MacroFetchWindow, result: ProviderFetchResult,
                      captured_at: datetime, run_id: str) -> str:
        captured_at = _require_aware_datetime(captured_at, "captured_at")
        request = sanitize_macro_value({"provider": result.provider, "seriesId": series.id,
            "source": source.model_dump(mode="json", by_alias=True),
            "window": {"start": window.start.isoformat(), "end": window.end.isoformat(), "asOf": window.as_of.isoformat()}})
        request_hash = _canonical_hash(request)
        batch_id = str(uuid5(NAMESPACE_URL, f"macro:{run_id}:{request_hash}"))
        audit = {**_audit_envelope(series, result), "request": request}
        # Commit raw independently; a later core transaction failure must not erase it.
        with self._session_factory.begin() as session:
            if session.get(IngestionBatch, batch_id) is not None:
                return batch_id
            session.add(IngestionBatch(id=batch_id, source=result.provider, dataset=f"macro:{series.id}",
                mode="macro", business_date=captured_at, started_at=captured_at, finished_at=captured_at,
                status="failed" if result.status in {"failed", "unavailable"} else "completed",
                request_count=result.request_count, row_count=0, rejected_count=0))
            session.flush()
            session.add(SourcePayload(batch_id=batch_id, api_name=f"macro:{result.provider}",
                request_hash=request_hash, content_hash=_canonical_hash(audit), payload=audit, captured_at=captured_at))
        return batch_id

    def latest_visible(
        self, series: MacroSeriesDefinition, as_of: datetime, end: date,
        *, provider: str | None = None,
    ) -> tuple[str, date, datetime] | None:
        cutoff = _require_aware_datetime(as_of, "as_of")
        priorities = {source.provider: source.priority for source in series.sources}
        visible = (
            MacroObservation.series_id == series.id,
            MacroObservation.observed_at <= end,
            MacroObservation.released_at <= cutoff,
        )
        with self._session_factory() as session:
            statement = select(MacroObservation).where(*visible)
            if provider is not None:
                statement = statement.where(MacroObservation.source == provider)
            candidates = session.scalars(
                statement
            ).all()
            verify_import_lineage(session, candidates)
        selected = select_visible_observations(candidates, cutoff, {series.id: tuple(priorities)})
        if not selected:
            return None
        chosen = max(selected, key=lambda row: (row.observed_at, _normalize_datetime(row.released_at)))
        return chosen.source, chosen.observed_at, _normalize_datetime(chosen.released_at)

    def persist(
        self,
        series: MacroSeriesDefinition,
        result: ProviderFetchResult,
        captured_at: datetime,
        *, batch_id: str | None = None,
    ) -> MacroPersistResult:
        captured_at = _require_aware_datetime(captured_at, "captured_at")
        audit = _audit_envelope(series, result)
        request_hash = _request_hash(series, result)

        with self._session_factory.begin() as session:
            _acquire_observation_locks(session, series, result)
            batch = session.get(IngestionBatch, batch_id) if batch_id is not None else None
            if batch_id is not None and (batch is None or batch.dataset != f"macro:{series.id}" or batch.source != result.provider):
                raise ValueError("MACRO_AUDIT_BATCH_MISMATCH")
            if batch is None:
                batch = IngestionBatch(
                    source=result.provider,
                    dataset=f"macro:{series.id}",
                    mode="macro",
                    business_date=captured_at,
                    status="running",
                    started_at=captured_at,
                    request_count=result.request_count,
                    row_count=0,
                    rejected_count=0,
                )
                session.add(batch)
                session.flush()
                session.add(SourcePayload(
                    batch_id=batch.id,
                    api_name=f"macro:{result.provider}",
                    request_hash=request_hash,
                    content_hash=_canonical_hash(audit),
                    payload=audit,
                    captured_at=captured_at,
                ))

            inserted = 0
            unchanged = 0
            rejected = 0
            effective_rows: list[tuple[date, datetime]] = []
            for row_number, observation in enumerate(result.observations, start=1):
                decision = self._persist_observation(
                    session,
                    series,
                    result,
                    observation,
                    captured_at,
                )
                if decision.status == "inserted":
                    inserted += 1
                elif decision.status == "unchanged":
                    unchanged += 1
                else:
                    rejected += 1
                    session.add(
                        RejectedRecord(
                            batch_id=batch.id,
                            row_number=row_number,
                            error_path="observations",
                            reason=decision.reason or "MACRO_OBSERVATION_INVALID",
                            raw_record=_observation_audit(observation),
                        )
                    )
                    continue
                if decision.observed_at is not None and decision.released_at is not None:
                    effective_rows.append(
                        (decision.observed_at, decision.released_at)
                    )

            batch.status = "completed" if batch.rejected_count + rejected == 0 else "partial"
            batch.finished_at = captured_at
            batch.row_count += inserted
            batch.rejected_count += rejected
            session.flush()

            latest_observed_at = max(
                (observed_at for observed_at, _released_at in effective_rows),
                default=None,
            )
            latest_released_at = max(
                (released_at for _observed_at, released_at in effective_rows),
                default=None,
            )
            return MacroPersistResult(
                batch_id=batch.id,
                received=len(result.observations),
                inserted=inserted,
                unchanged=unchanged,
                rejected=rejected,
                latest_observed_at=latest_observed_at,
                latest_released_at=latest_released_at,
            )

    def _persist_observation(
        self,
        session: Session,
        series: MacroSeriesDefinition,
        result: ProviderFetchResult,
        observation: ProviderObservation,
        captured_at: datetime,
    ) -> _PersistDecision:
        reason = _invalid_reason(result, observation)
        if reason is not None:
            return _PersistDecision("rejected", reason=reason)

        released_at = (
            _normalize_datetime(observation.released_at)
            if observation.released_at is not None
            else captured_at
        )
        revision = observation.revision.strip() if observation.revision else None
        if revision is not None:
            existing = session.scalar(
                select(MacroObservation)
                .where(
                    MacroObservation.source == observation.provider,
                    MacroObservation.series_id == series.id,
                    MacroObservation.observed_at == observation.observed_at,
                    MacroObservation.revision == revision,
                )
                .order_by(MacroObservation.released_at.desc())
            )
            if existing is not None:
                if existing.value != observation.value:
                    return _PersistDecision(
                        "rejected",
                        reason="PROVIDER_REVISION_VALUE_CONFLICT",
                    )
                # An initially open revision may later be closed. Historical
                # replay must never reopen it or discard an earlier known end.
                if observation.valid_until is not None:
                    until = _normalize_datetime(observation.valid_until)
                    if existing.valid_until is None or until < _normalize_datetime(existing.valid_until):
                        existing.valid_until = until
                return _PersistDecision(
                    "unchanged",
                    observation.observed_at,
                    _normalize_datetime(existing.released_at),
                )
        else:
            latest = session.scalar(
                select(MacroObservation)
                .where(
                    MacroObservation.source == observation.provider,
                    MacroObservation.series_id == series.id,
                    MacroObservation.observed_at == observation.observed_at,
                )
                .order_by(MacroObservation.released_at.desc(), MacroObservation.id.desc())
            )
            if latest is not None and latest.value == observation.value:
                return _PersistDecision(
                    "unchanged",
                    observation.observed_at,
                    _normalize_datetime(latest.released_at),
                )
            revision = _synthetic_revision(
                "detected" if latest is not None else "initial-import",
                series,
                observation,
            )

        session.add(
            MacroObservation(
                source=observation.provider,
                series_id=series.id,
                name=series.name,
                region=series.region,
                frequency=series.frequency,
                unit=series.unit,
                seasonal_adjustment=series.seasonal_adjustment,
                observed_at=observation.observed_at,
                released_at=released_at,
                revision=revision,
                value=observation.value,
                source_url=observation.source_url,
                valid_until=observation.valid_until,
            )
        )
        session.flush()
        return _PersistDecision(
            "inserted",
            observation.observed_at,
            released_at,
        )


def _invalid_reason(
    result: ProviderFetchResult,
    observation: ProviderObservation,
) -> str | None:
    if result.status != "success":
        return "RESULT_STATUS_INCONSISTENT"
    if observation.provider != result.provider:
        return "PROVIDER_MISMATCH"
    if not isinstance(observation.value, Decimal) or not observation.value.is_finite():
        return "NON_FINITE_VALUE"
    if isinstance(observation.observed_at, datetime) or not isinstance(
        observation.observed_at,
        date,
    ):
        return "OBSERVED_AT_INVALID"
    if observation.released_at is not None and (
        not isinstance(observation.released_at, datetime)
        or observation.released_at.utcoffset() is None
    ):
        return "RELEASED_AT_TIMEZONE_REQUIRED"
    if observation.valid_until is not None and (
        not isinstance(observation.valid_until, datetime)
        or observation.valid_until.utcoffset() is None
        or observation.released_at is None
        or observation.valid_until <= observation.released_at
    ):
        return "VALID_UNTIL_INVALID"
    if not observation.provider_series_id.strip():
        return "PROVIDER_SERIES_ID_REQUIRED"
    if not observation.source_url.startswith("https://"):
        return "SOURCE_URL_HTTPS_REQUIRED"
    if observation.revision is not None and (
        not observation.revision.strip() or len(observation.revision.strip()) > 64
    ):
        return "REVISION_INVALID"
    return None


def _synthetic_revision(
    prefix: str,
    series: MacroSeriesDefinition,
    observation: ProviderObservation,
) -> str:
    fingerprint = {
        "source": observation.provider,
        "seriesId": series.id,
        "providerSeriesId": observation.provider_series_id,
        "observedAt": observation.observed_at.isoformat(),
        "value": str(observation.value),
    }
    return f"{prefix}:{_canonical_hash(fingerprint)[:16]}"


def _request_hash(
    series: MacroSeriesDefinition,
    result: ProviderFetchResult,
) -> str:
    provider_series_ids = sorted(
        {
            observation.provider_series_id
            for observation in result.observations
            if observation.provider == result.provider
            and observation.provider_series_id.strip()
        }
    )
    return _canonical_hash(
        {
            "provider": result.provider,
            "seriesId": series.id,
            "providerSeriesIds": provider_series_ids,
        }
    )


def _audit_envelope(
    series: MacroSeriesDefinition,
    result: ProviderFetchResult,
) -> dict[str, object]:
    raw = sanitize_macro_value(result.raw_payload)
    if not isinstance(raw, dict):
        raw = {"value": raw}
    common: dict[str, object] = {
        "provider": result.provider,
        "seriesId": series.id,
        "status": result.status,
        "requestCount": result.request_count,
    }
    file_type = raw.get("fileType")
    content_hash = raw.get("contentSha256")
    if isinstance(file_type, str) and file_type.casefold() in _FILE_TYPES:
        metadata = {
            key: value
            for key, value in raw.items()
            if key not in {"fileType", "contentSha256"}
        }
        return {
            "format": "file",
            "fileType": file_type.casefold(),
            "contentSha256": content_hash,
            **common,
            "metadata": metadata,
        }
    return {"format": "json", **common, "data": raw}


def _observation_audit(observation: ProviderObservation) -> dict[str, object]:
    audit = sanitize_macro_value({
        "provider": observation.provider,
        "providerSeriesId": observation.provider_series_id,
        "observedAt": (
            observation.observed_at.isoformat()
            if isinstance(observation.observed_at, date)
            else str(observation.observed_at)
        ),
        "releasedAt": (
            observation.released_at.isoformat()
            if isinstance(observation.released_at, datetime)
            else None
        ),
        "revision": observation.revision,
        "value": str(observation.value),
        "sourceUrl": observation.source_url,
        "validUntil": observation.valid_until.isoformat() if isinstance(observation.valid_until, datetime) else None,
    })
    if not isinstance(audit, dict):
        raise TypeError("observation audit must be a JSON object")
    return audit


def _acquire_observation_locks(
    session: Session,
    series: MacroSeriesDefinition,
    result: ProviderFetchResult,
) -> None:
    lock_keys = sorted(
        {
            _observation_lock_key(series, observation)
            for observation in result.observations
            if _invalid_reason(result, observation) is None
        }
    )
    for lock_key in lock_keys:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )


def _observation_lock_key(
    series: MacroSeriesDefinition,
    observation: ProviderObservation,
) -> int:
    identity = {
        "seriesId": series.id,
        "provider": observation.provider,
        "observedAt": observation.observed_at.isoformat(),
    }
    digest = bytes.fromhex(_canonical_hash(identity))
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _require_aware_datetime(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _normalize_datetime(value: datetime) -> datetime:
    if value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
