"""股票映射策略：仅解释已有原始数据，不采集、补造或持久化。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config_paths import CONFIG_ROOT

if TYPE_CHECKING:
    from app.db.models import Security
    from app.stocks.derived.inputs import CoreSnapshot

DEFAULT_STOCK_MAPPING_FILE = CONFIG_ROOT / "stocks/stock-mapping.json"


class InstitutionMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    includedTypes: list[str] = Field(min_length=1)
    excludedTypes: list[str]
    maxDenominatorLagDays: int = Field(ge=0, le=31)

    @model_validator(mode="after")
    def unique_types(self):
        included = [value.strip().casefold() for value in self.includedTypes]
        excluded = [value.strip().casefold() for value in self.excludedTypes]
        if (not all(included + excluded) or len(set(included)) != len(included)
            or len(set(excluded)) != len(excluded) or set(included) & set(excluded)):
            raise ValueError("股东类型必须非空、唯一且不重叠")
        return self


class ClassificationSyncConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    asOf: date | None = None
    maxRequestsPerRun: int = Field(default=128, gt=0)
    pageSize: int = Field(default=2000, ge=1, le=2000)


class ClassificationMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    provider: str = Field(min_length=1)
    source: str = Field(min_length=1)
    majorLevel: Literal[1] = 1
    subLevel: Literal[2, 3] = 3
    sync: ClassificationSyncConfig = Field(default_factory=ClassificationSyncConfig)

    @model_validator(mode="after")
    def valid_identity(self):
        if not self.provider.strip() or not self.source.strip():
            raise ValueError("分类来源身份不能只包含空白")
        return self


class StockMappingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schemaVersion: Literal[1]
    policyVersion: str = Field(min_length=1)
    institution: InstitutionMapping
    classification: ClassificationMapping

    @classmethod
    def load(cls, path: Path | None = None):
        path = path or DEFAULT_STOCK_MAPPING_FILE
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    @property
    def fingerprint(self) -> str:
        return sha256(self.model_dump_json().encode()).hexdigest()

    def holder_kind(self, value: object) -> str:
        normalized = str(value or "").strip().casefold()
        if normalized in {item.strip().casefold() for item in self.institution.includedTypes}:
            return "included"
        if normalized in {item.strip().casefold() for item in self.institution.excludedTypes}:
            return "excluded"
        return "unknown"


def finite_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def classification_source(node) -> str:
    source = getattr(node, "source", None)
    if source is not None:
        return source
    value = (getattr(node, "data", None) or {}).get("src")
    return value if isinstance(value, str) and value else "legacy_unknown"


def classification_fields(snapshot: CoreSnapshot, security: Security, policy: StockMappingPolicy | None = None):
    policy = policy or StockMappingPolicy.load()
    selection = policy.classification
    eligible = [node for node in snapshot.classifications
        if node.provider == selection.provider
        and classification_source(node) == selection.source]
    nodes = {node.id: node for node in eligible}
    by_code = {node.classification_id: node for node in eligible}
    levels: dict[int, set[str]] = {}
    active = [member for member in snapshot.classification_members
        if member.ts_code == security.ts_code and member.effective_from <= snapshot.as_of
        and (member.effective_to is None or member.effective_to >= snapshot.as_of)
        and member.classification_id in nodes]
    invalid_chain = False
    for member in active:
        node = nodes[member.classification_id]
        visited = set()
        while node is not None:
            if node.classification_id in visited:
                invalid_chain = True
                break
            visited.add(node.classification_id)
            if node.level in (1, 2, 3) and isinstance(node.name, str) and node.name.strip():
                levels.setdefault(node.level, set()).add(node.classification_id)
            if node.level == 1 and node.parent_id in (None, "0", ""):
                break
            if node.parent_id is None:
                invalid_chain = True
                break
            parent = by_code.get(node.parent_id)
            if parent is None or node.level is None or parent.level != node.level - 1:
                invalid_chain = True
                break
            node = parent
    conflict = any(len(values) > 1 for values in levels.values())
    def selected(level):
        keys = levels.get(level, set())
        return by_code[next(iter(keys))].name if len(keys) == 1 and not conflict and not invalid_chain else ""
    major, sub = selected(selection.majorLevel), selected(selection.subLevel)
    values = {"majorIndustry": major, "subIndustry": sub, "industry": major or "",
        "concepts": [], "region": (security.data or {}).get("region") or (security.data or {}).get("area") or ""}
    reason = ("classification_conflict" if conflict else "classification_invalid_chain" if invalid_chain
        else "classification_members_missing" if not active else "classification_partial" if not major or not sub else None)
    issues = () if reason is None else ({"source": "core.classification_member", "symbol": security.ts_code,
        "reason": reason, "provider": selection.provider, "classificationSource": selection.source,
        "policyVersion": policy.policyVersion, "policyHash": policy.fingerprint},)
    return values, issues
