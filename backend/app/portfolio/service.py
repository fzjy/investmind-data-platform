from __future__ import annotations

from app.portfolio.analysis import analyze_portfolio, load_analysis_settings, validate_request
from app.portfolio.normalizer import normalize_portfolio
from app.portfolio.provider import PortfolioProvider
from app.shared.contracts import DataResult, success_result


class PortfolioService:
    def __init__(self, provider: PortfolioProvider) -> None:
        self._provider = provider

    def get_portfolio(self) -> DataResult[dict[str, object]]:
        snapshot = self._provider.read_portfolio()
        return success_result(normalize_portfolio(snapshot.raw), snapshot.meta)

    def analyze_portfolio(self, request: object) -> DataResult[dict[str, object]]:
        settings = load_analysis_settings()
        validated = validate_request(request, settings)
        snapshot = self._provider.read_portfolio()
        return success_result(analyze_portfolio(normalize_portfolio(snapshot.raw), validated, settings), snapshot.meta)
