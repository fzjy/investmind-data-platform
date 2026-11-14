from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


UniverseProfile = Literal["representative-50", "custom", "all"]
UniverseCoverage = Literal["representative", "custom", "full-market"]


@dataclass(frozen=True, slots=True)
class UniverseSecurityState:
    status: str | None
    is_st: bool
    board: str | None = None
    industry: str | None = None


@dataclass(frozen=True, slots=True)
class UniverseSelection:
    profile: UniverseProfile
    version: str
    coverage: UniverseCoverage
    symbols: tuple[str, ...]
    fingerprint: str
    warnings: tuple[str, ...] = ()

    def contains(self, symbol: str) -> bool:
        return symbol in self.symbols

    def to_payload(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "version": self.version,
            "size": len(self.symbols),
            "coverage": self.coverage,
            "fingerprint": self.fingerprint,
            "warnings": list(self.warnings),
        }
