"""供应商当前证据的历史有效分类重建，不提供公开可知时间保证。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from app.ingestion.normalizers import normalize_classification

MEMBER_FIELDS = ("l1_code", "l1_name", "l2_code", "l2_name", "l3_code", "l3_name",
    "ts_code", "name", "in_date", "out_date", "is_new")


@dataclass(frozen=True)
class Catalog:
    nodes: dict[str, dict[str, Any]]
    aliases: dict[str, str]
    source: str

    @classmethod
    def from_rows(cls, rows: Iterable[dict[str, Any]], source: str = "SW2021") -> Catalog:
        nodes = {}
        aliases = {}
        for row in rows:
            if row.get("src") != source:
                raise ValueError("catalog source mismatch")
            normalized = normalize_classification(row, source=source)
            code = normalized["classification_id"]
            if normalized["level"] not in (1, 2, 3) or not normalized["name"].strip():
                raise ValueError("invalid catalog level/name")
            if code in nodes and nodes[code] != normalized:
                raise ValueError("conflicting catalog node")
            nodes[code] = normalized
            for alias in (code, row.get("industry_code")):
                if alias not in (None, ""):
                    alias = str(alias)
                    if alias in aliases and aliases[alias] != code:
                        raise ValueError("ambiguous catalog alias")
                    aliases[alias] = code
        if {node["level"] for node in nodes.values()} != {1, 2, 3}:
            raise ValueError("catalog requires all three levels")
        for node in nodes.values():
            parent = node["parent_id"]
            if node["level"] == 1:
                if parent not in (None, "", "0", 0):
                    raise ValueError("root catalog parent must be empty")
                node["parent_id"] = None
            else:
                code = aliases.get(str(parent))
                if code is None or nodes[code]["level"] != node["level"] - 1:
                    raise ValueError("catalog parent missing or wrong level")
                node["parent_id"] = code
        return cls(nodes, aliases, source)

    def path(self, row: dict[str, Any]) -> tuple[str, str, str]:
        codes = []
        for level in (1, 2, 3):
            code = self.aliases.get(str(row.get(f"l{level}_code")))
            if code is None:
                raise ValueError("member catalog code missing")
            node = self.nodes[code]
            if node["level"] != level or row.get(f"l{level}_name") != node["name"]:
                raise ValueError("member catalog level/name mismatch")
            if level > 1 and node["parent_id"] != codes[-1]:
                raise ValueError("member catalog path mismatch")
            codes.append(code)
        return tuple(codes)  # type: ignore[return-value]


@dataclass(frozen=True)
class MemberRecord:
    symbol: str
    path: tuple[str, str, str]
    effective_from: date
    effective_to: date | None
    out_date: date | None
    is_new: str
    row_number: int


class MemberValidationError(ValueError):
    def __init__(self, message: str, row_number: int, raw_record: dict[str, Any], is_new: str):
        super().__init__(message)
        self.row_number, self.raw_record, self.is_new = row_number, raw_record, is_new


def _date(value: object) -> date:
    if not isinstance(value, str) or len(value) != 8 or not value.isascii() or not value.isdigit():
        raise ValueError("invalid member date")
    return date(int(value[:4]), int(value[4:6]), int(value[6:]))


def merge_members(records: Iterable[MemberRecord]) -> list[MemberRecord]:
    unique = {}
    for record in records:
        key = (record.symbol, record.path[-1], record.effective_from)
        prior = unique.get(key)
        if prior is not None and (prior.path, prior.effective_to) != (record.path, record.effective_to):
            raise ValueError("conflicting member intervals")
        unique[key] = record
    ordered = sorted(unique.values(), key=lambda record: (record.symbol, record.effective_from))
    for previous, current in zip(ordered, ordered[1:]):
        if previous.symbol == current.symbol and (previous.effective_to is None or previous.effective_to >= current.effective_from):
            raise ValueError("overlapping member intervals")
    return ordered


def normalize_members(rows: Iterable[dict[str, Any]], *, catalog: Catalog, symbol: str,
    is_new: str, window_start: date, as_of: date, retain_outside_window: bool = False) -> list[MemberRecord]:
    records = []
    for row_number, row in enumerate(rows, start=1):
        try:
            if row.get("ts_code") != symbol:
                raise ValueError("member outside requested stock pool unit")
            if is_new not in ("Y", "N") or row.get("is_new") != is_new:
                raise ValueError("member is_new mismatch")
            path = catalog.path(row)
            start = _date(row.get("in_date"))
            raw_end = row.get("out_date")
            end = _date(raw_end) if raw_end not in (None, "") else None
            if end is not None and end <= start:
                raise ValueError("invalid member date interval")
            if is_new == "N" and end is None:
                raise ValueError("historical member terminal unknown")
        except ValueError as exc:
            raise MemberValidationError(str(exc), row_number, row, is_new) from exc
        records.append(MemberRecord(symbol, path, start, end - timedelta(days=1) if end else None, end, is_new, row_number))
    validated = merge_members(records)
    if retain_outside_window:
        return validated
    return [record for record in validated
        if record.effective_from <= as_of and (record.effective_to is None or record.effective_to >= window_start)]
