from __future__ import annotations

from typing import Protocol

from app.shared.contracts import ProviderSnapshot


class MacroProvider(Protocol):
    def read_transmission(self, version: str | None = None) -> ProviderSnapshot[dict[str, object]]: ...

    def read_insights(self, version: str | None = None) -> ProviderSnapshot[dict[str, object]]: ...
