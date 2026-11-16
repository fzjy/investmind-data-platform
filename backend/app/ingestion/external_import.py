from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.external_files.contracts import ContentImportRecord, MacroImportRecord
from app.connectors.external_files.reader import ExternalFileReader, ImportValidationFailed
from app.db.models import ContentDocument, ExternalFile, ExternalRecord, MacroObservation
from app.content.content_mapping import resolve_content_securities
from app.content.content_policy import permitted_text


@dataclass(frozen=True)
class ImportPreview:
    file_hash: str
    row_count: int
    macro_count: int
    content_count: int
    errors: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ImportSummary:
    status: str
    file_id: int | None
    file_hash: str
    row_count: int
    imported_count: int
    rejected_count: int
    affected_products: tuple[str, ...]


def _parse(reader: ExternalFileReader, path: Path) -> tuple[list[MacroImportRecord | ContentImportRecord], list[dict[str, Any]]]:
    try:
        return list(reader.read(path)), []
    except ImportValidationFailed as exc:
        return [], list(exc.errors)


def validate_file(reader: ExternalFileReader, path: Path) -> ImportPreview:
    records, errors = _parse(reader, path)
    return ImportPreview(
        ExternalImporter.file_hash(path), len(records),
        sum(isinstance(row, MacroImportRecord) for row in records),
        sum(isinstance(row, ContentImportRecord) for row in records), tuple(errors),
    )


class ExternalImporter:
    def __init__(self, session_factory: sessionmaker[Session], reader: ExternalFileReader) -> None:
        self._session_factory = session_factory
        self._reader = reader

    @staticmethod
    def file_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def validate(self, path: Path) -> ImportPreview:
        return validate_file(self._reader, path)

    def _security_errors(self, session: Session, records: list[MacroImportRecord | ContentImportRecord]) -> list[dict[str, Any]]:
        content = [(index, row) for index, row in enumerate(records, 1) if isinstance(row, ContentImportRecord)]
        return resolve_content_securities(session, [row for _, row in content],
            record_numbers=[index for index, _ in content]) if content else []

    @staticmethod
    def _affected_products(records: list[MacroImportRecord | ContentImportRecord]) -> tuple[str, ...]:
        products: list[str] = []
        if any(isinstance(row, MacroImportRecord) for row in records):
            products.extend(("macro-transmission", "macro-insights"))
        content = [row for row in records if isinstance(row, ContentImportRecord)]
        if content:
            products.append("market-explorer")
            products.extend(f"stock-workbench:{symbol}" for symbol in sorted({symbol for row in content for symbol in row.securities}))
        return tuple(products)

    def import_file(self, path: Path) -> ImportSummary:
        file_hash = self.file_hash(path)
        with self._session_factory() as session:
            existing = session.scalar(select(ExternalFile).where(ExternalFile.file_hash == file_hash))
            if existing is not None:
                if existing.status == "rejected":
                    raise ImportValidationFailed(existing.error_summary)
                stored = session.scalars(select(ExternalRecord).where(ExternalRecord.external_file_id == existing.id)).all()
                previous = [MacroImportRecord.model_validate(row.payload) if row.record_type == "macro" else ContentImportRecord.model_validate(row.payload) for row in stored]
                return ImportSummary("already_imported", existing.id, file_hash, existing.row_count, existing.valid_count, existing.rejected_count, self._affected_products(previous))
        records, errors = _parse(self._reader, path)
        rejected_file_id: int | None = None
        if errors:
            with self._session_factory.begin() as session:
                file_row = ExternalFile(path_name=path.name, file_hash=file_hash, schema_version="1.0", byte_size=path.stat().st_size, row_count=len(records) + len(errors), valid_count=0, rejected_count=len(errors), status="rejected", error_summary=errors)
                session.add(file_row)
                session.flush()
                rejected_file_id = file_row.id
            raise ImportValidationFailed(errors)
        with self._session_factory.begin() as session:
            security_errors = self._security_errors(session, records)
            if security_errors:
                errors.extend(security_errors)
            file_row = ExternalFile(path_name=path.name, file_hash=file_hash, schema_version="1.0", byte_size=path.stat().st_size, row_count=len(records) + len(errors), valid_count=0 if errors else len(records), rejected_count=len(errors), status="rejected" if errors else "imported", error_summary=errors)
            session.add(file_row)
            session.flush()
            for row_number, record in enumerate(records, 1):
                if isinstance(record, ContentImportRecord):
                    text = permitted_text(record.copyright_status, record.summary, record.body)
                    record.summary, record.body = text["summary"], text["body"]
                session.add(ExternalRecord(external_file_id=file_row.id, row_number=row_number, record_type="macro" if isinstance(record, MacroImportRecord) else "content", payload=record.model_dump(mode="json", by_alias=True)))
            if errors:
                rejected_file_id = file_row.id
            if errors:
                # The transaction must finish before exposing the validation error.
                pass
            else:
                for row in records:
                    if isinstance(row, MacroImportRecord):
                        existing_row = session.scalar(select(MacroObservation).where(MacroObservation.source == row.source, MacroObservation.series_id == row.series_id, MacroObservation.observed_at == row.observed_at, MacroObservation.released_at == row.released_at, MacroObservation.revision == row.revision))
                        values = dict(source=row.source, series_id=row.series_id, name=row.name, region=row.region, frequency=row.frequency, unit=row.unit, seasonal_adjustment=row.seasonal_adjustment, observed_at=row.observed_at, released_at=row.released_at, revision=row.revision, value=row.value, source_url=str(row.source_url))
                        if existing_row is None:
                            session.add(MacroObservation(**values))
                        else:
                            for key, value in values.items():
                                setattr(existing_row, key, value)
                    else:
                        dedupe_key = f"{row.source}:{row.external_id}" if row.external_id else hashlib.sha256(f"{row.source}|{row.title.strip().lower()}|{row.published_at.isoformat()}".encode()).hexdigest()
                        existing_row = session.scalar(select(ContentDocument).where(ContentDocument.dedupe_key == dedupe_key))
                        values = dict(source=row.source, external_id=row.external_id, dedupe_key=dedupe_key, content_type=row.content_type, title=row.title, summary=row.summary, body=row.body, published_at=row.published_at, source_url=str(row.source_url), securities=row.securities, copyright_status=row.copyright_status)
                        if existing_row is None:
                            session.add(ContentDocument(**values))
                        else:
                            for key, value in values.items():
                                setattr(existing_row, key, value)
                file_row.imported_at = datetime.now(timezone.utc)
                from app.acquisition.adapters.imports import record_import
                sources = [('external-macro',row.source,row.series_id) if isinstance(row,MacroImportRecord)
                           else ('external-content',row.source,row.content_type) for row in records]
                record_import(session, sources, file_row, file_row.imported_at)
                result = ImportSummary("imported", file_row.id, file_hash, len(records), len(records), 0, self._affected_products(records))
            if errors:
                result = ImportSummary("rejected", rejected_file_id, file_hash, len(records) + len(errors), 0, len(errors), ())
        if errors:
            raise ImportValidationFailed(errors)
        return result
