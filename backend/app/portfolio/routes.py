from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.portfolio.provider import PortfolioProvider
from app.portfolio.analysis import invalid_request
from app.portfolio.service import PortfolioService
from app.shared.contracts import DataResult


def _json_result(result: DataResult[dict[str, object]]) -> JSONResponse:
    return JSONResponse(result.model_dump(mode="json", by_alias=True))


def create_router(service: PortfolioService | None = None, provider: PortfolioProvider | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/data")
    if service is None:
        if provider is None:
            raise ValueError("Portfolio routes require an explicit service or provider")
        service = PortfolioService(provider)
    active_service = service

    @router.get("/portfolio")
    async def portfolio() -> JSONResponse:
        return _json_result(active_service.get_portfolio())

    @router.post("/portfolio/analysis")
    async def portfolio_analysis(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            invalid_request("请求体必须为有效 JSON")
        return _json_result(active_service.analyze_portfolio(payload))

    return router
