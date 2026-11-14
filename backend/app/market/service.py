from __future__ import annotations

from app.market.normalizer import normalize_market_explorer, normalize_market_overview
from app.market.provider import MarketProvider
from app.shared.contracts import DataResult, success_result


class MarketService:
    def __init__(self, provider: MarketProvider) -> None:
        self._provider = provider

    def get_overview(self) -> DataResult[dict[str, object]]:
        snapshot = self._provider.read_overview()
        return success_result(normalize_market_overview(snapshot.raw), snapshot.meta)

    def get_explorer(self) -> DataResult[dict[str, object]]:
        snapshot = self._provider.read_explorer()
        return success_result(normalize_market_explorer(snapshot.raw), snapshot.meta)
