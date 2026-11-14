from __future__ import annotations


from app.portfolio.cache_validator import matches_etag, portfolio_etag
from app.portfolio.normalizer import legacy_portfolio, normalize_portfolio
from app.portfolio.provider import PortfolioProvider
from app.shared.contracts import DataResult, success_result


class PortfolioService:
    def __init__(self, provider: PortfolioProvider) -> None:
        self._provider = provider

    def conditional_etag(self, condition: str) -> str | None:
        read_meta = getattr(self._provider, "read_portfolio_meta", None)
        if read_meta is None:
            return None
        tag = portfolio_etag(read_meta())
        return tag if matches_etag(condition, tag) else None

    def get_portfolio(self, *, legacy: bool = False) -> DataResult[dict[str, object]]:
        snapshot = self._provider.read_portfolio()
        data = legacy_portfolio(snapshot.raw, snapshot.meta.source) if legacy else normalize_portfolio(snapshot.raw)
        return success_result(data, snapshot.meta)
