from __future__ import annotations


from app.portfolio.normalizer import normalize_portfolio
from app.portfolio.provider import PortfolioProvider
from app.shared.contracts import DataResult, success_result


class PortfolioService:
    def __init__(self, provider: PortfolioProvider) -> None:
        self._provider = provider

    def get_portfolio(self) -> DataResult[dict[str, object]]:
        snapshot = self._provider.read_portfolio()
        return success_result(normalize_portfolio(snapshot.raw), snapshot.meta)
