from __future__ import annotations


from app.portfolio.normalizer import legacy_portfolio, normalize_portfolio
from app.portfolio.provider import PortfolioProvider
from app.shared.contracts import DataResult, success_result


class PortfolioService:
    def __init__(self, provider: PortfolioProvider) -> None:
        self._provider = provider

    def get_portfolio(self, *, legacy: bool = False) -> DataResult[dict[str, object]]:
        snapshot = self._provider.read_portfolio()
        data = legacy_portfolio(snapshot.raw, snapshot.meta.source) if legacy else normalize_portfolio(snapshot.raw)
        return success_result(data, snapshot.meta)
