from __future__ import annotations

from typing import Protocol

from app.shared.contracts import ProviderSnapshot


class StocksProvider(Protocol):
    def read_screener_universe(self) -> ProviderSnapshot[dict[str, object]]: ...

    def read_workbench(self, symbol: str) -> ProviderSnapshot[dict[str, object] | None]: ...
