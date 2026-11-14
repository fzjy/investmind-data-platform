from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from collections.abc import Mapping
from typing import Literal

from app.shared.fingerprints import canonical_source_hash


@dataclass(frozen=True)
class MartRows:
    screener: tuple[dict[str, object], ...] = ()
    workbenches: tuple[dict[str, object], ...] = ()
    charts: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class MartCandidate:
    product: str
    as_of: datetime
    payload: dict[str, object]
    rows: MartRows = MartRows()
    missing_data: tuple[dict[str, object], ...] = ()
    stale: bool = False
    stale_reason: str | None = None
    metric_card_signature: str | None = None
    workbench_parent_version: str | None = None
    risk_input_signature: str | None = None
    shareholder_return_signature: str | None = None
    shareholder_return_signature: str | None = None


@dataclass(frozen=True)
class QualityResult:
    name: str
    severity: Literal["blocking", "advisory"]
    passed: bool
    details: dict[str, object]


@dataclass(frozen=True)
class QualityContext:
    eligible_security_count: int
    requested_workbenches: tuple[str, ...]
    declared_lineage_count: int
    persisted_lineage_count: int
    missing_sources: tuple[str, ...]
    source_duplicate_keys: tuple[str, ...] = ()
    source_quality_results: tuple[QualityResult, ...] = ()
    expected_screener_symbols: tuple[str, ...] | None = None
    baseline_screener_rows: tuple[dict[str, object], ...] | None = None
    max_row_drop_ratio: float = 0.2
    max_field_drop_ratio: float = 0.2


def release_version(
    as_of: date | datetime,
    candidates: tuple[MartCandidate, ...] | list[MartCandidate],
) -> str:
    material = tuple(
        (
            candidate.product,
            _stable_release_value(candidate.payload),
            _stable_release_value(asdict(candidate.rows)),
            _stable_release_value(candidate.missing_data),
        )
        for candidate in sorted(candidates, key=lambda item: item.product)
    )
    digest = canonical_source_hash(
        material,
        "mart.release",
        as_of.isoformat(),
    )
    return f"{as_of:%Y%m%d}-{digest[:16]}"


def _stable_release_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _stable_release_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_stable_release_value(item) for item in value]
    return value


__all__ = [
    "MartCandidate",
    "MartRows",
    "QualityContext",
    "QualityResult",
    "release_version",
]
