from __future__ import annotations

from typing import Protocol

from app.shared.contracts import ProviderSnapshot


class MarketProvider(Protocol):
    def read_overview(self) -> ProviderSnapshot[dict[str, object]]: ...

    def read_explorer(self) -> ProviderSnapshot[dict[str, object]]: ...
