from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, ROUND_HALF_UP, localcontext

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Session

from app.connectors.external_files.contracts import MacroImportRecord
from app.db.models import ExternalFile, ExternalRecord, MacroObservation


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _signature(row: MacroObservation | MacroImportRecord, value: Decimal) -> tuple[object, ...]:
    return (row.source, row.series_id, row.name, row.region, row.frequency, row.unit,
            row.seasonal_adjustment, row.observed_at, _utc(row.released_at),
            row.revision, value, str(row.source_url))


def _stored_import_value(value: Decimal, dialect: Dialect) -> Decimal:
    """Compare the ledger with what the core column actually stores/reads.

    SQLite's Numeric path converts through float; PostgreSQL keeps Decimal
    and rounds numeric ties away from zero. Do not round the observed core
    value again: a change to any representable stored digit must still fail.
    """
    column_type = MacroObservation.__table__.c.value.type
    if dialect.name == "postgresql":
        with localcontext() as context:
            context.prec = max(column_type.precision, len(value.as_tuple().digits) + column_type.scale)
            return value.quantize(Decimal(1).scaleb(-column_type.scale), rounding=ROUND_HALF_UP)
    bind = column_type.bind_processor(dialect)
    result = column_type.result_processor(dialect, None)
    bound = bind(value) if bind else value
    return result(bound) if result else bound


def is_verified_import(row: MacroObservation) -> bool:
    return getattr(row, "_macro_import_verified", False) is True


def verify_import_lineage(session: Session, rows: Sequence[MacroObservation]) -> None:
    """Trust current stored values backed by a successful controlled import ledger.

    This transient marker is recomputed on each database read, never persisted or
    accepted from provider payloads. Legacy rows without matching ledger stay untrusted.
    """
    if not rows:
        return
    payloads = session.scalars(select(ExternalRecord.payload).join(
        ExternalFile, ExternalFile.id == ExternalRecord.external_file_id,
    ).where(ExternalFile.status == "imported", ExternalFile.imported_at.is_not(None),
            ExternalRecord.record_type == "macro")).all()
    signatures: set[tuple[object, ...]] = set()
    dialect = session.get_bind(mapper=MacroObservation).dialect
    for payload in payloads:
        try:
            record = MacroImportRecord.model_validate(payload)
            signatures.add(_signature(record, _stored_import_value(record.value, dialect)))
        except (ValidationError, DecimalException, ValueError, OverflowError):
            continue
    for row in rows:
        row._macro_import_verified = _signature(row, row.value) in signatures
