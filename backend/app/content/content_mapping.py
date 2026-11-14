"""Resolve explicit hints against an exact, unique security directory."""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.external_files.contracts import ContentImportRecord
from app.db.models import Security, SecurityNameHistory


def resolve_content_securities(session: Session, records: list[ContentImportRecord], *,
                              record_numbers: list[int] | None = None) -> list[dict[str, object]]:
    codes: dict[str, set[str]] = defaultdict(set)
    names: dict[str, set[str]] = defaultdict(set)
    for security in session.scalars(select(Security)):
        codes[security.ts_code].add(security.ts_code)
        codes[security.symbol].add(security.ts_code)
        names[security.name].add(security.ts_code)
        fullname = (security.data or {}).get("fullname")
        if isinstance(fullname, str) and fullname:
            names[fullname].add(security.ts_code)
    for history in session.scalars(select(SecurityNameHistory)):
        if history.ts_code in codes:
            names[history.name].add(history.ts_code)
    errors: list[dict[str, object]] = []
    numbers = record_numbers if record_numbers is not None else list(range(1, len(records) + 1))
    for index, record in zip(numbers, records, strict=True):
        linked: set[str] = set()
        for hint in record.securities:
            candidates = codes.get(hint, set())
            if not candidates:
                errors.append({"record": index, "errors": [{"message": f"证券不存在: {hint}"}]})
            elif len(candidates) == 1:
                linked.update(candidates)
        for hint in record.security_names:
            candidates = names.get(hint, set())
            if len(candidates) == 1:
                linked.update(candidates)
        record.securities = sorted(linked)
    return errors
