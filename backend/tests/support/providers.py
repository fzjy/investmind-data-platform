from __future__ import annotations

from app.shared.contracts import ProviderSnapshot
from backend.tests.support.fixtures import FixtureStore


class MarketFixtureProvider:
    def __init__(self, store: FixtureStore) -> None:
        self._store = store

    def read_overview(self) -> ProviderSnapshot[dict[str, object]]:
        return self._store.read("market-overview")

    def read_explorer(self) -> ProviderSnapshot[dict[str, object]]:
        return self._store.read("market-explorer")


class MacroFixtureProvider:
    def __init__(self, store: FixtureStore) -> None:
        self._store = store

    def read_transmission(self, version: str | None = None) -> ProviderSnapshot[dict[str, object]]:
        if version is not None:
            from app.shared.errors import DataServiceError
            raise DataServiceError("INVALID_REQUEST", "固定版本读取仅适用于 Database 模式")
        return self._store.read("macro-transmission")

    def read_insights(self, version: str | None = None) -> ProviderSnapshot[dict[str, object]]:
        if version is not None:
            from app.shared.errors import DataServiceError
            raise DataServiceError("INVALID_REQUEST", "固定版本读取仅适用于 Database 模式")
        return self._store.read("macro-insights")


class PortfolioFixtureProvider:
    def __init__(self, store: FixtureStore) -> None:
        self._store = store

    def read_portfolio(self) -> ProviderSnapshot[dict[str, object]]:
        return self._store.read("portfolio")


class StocksFixtureProvider:
    def __init__(self, store: FixtureStore) -> None:
        self._store = store

    def read_screener_universe(self) -> ProviderSnapshot[dict[str, object]]:
        return self._store.read("screener")

    def read_workbench(self, symbol: str) -> ProviderSnapshot[dict[str, object] | None]:
        snapshot = self._store.read("stock-workbench")
        value = snapshot.raw.get(symbol)
        return ProviderSnapshot(value if isinstance(value, dict) else None, snapshot.meta)
