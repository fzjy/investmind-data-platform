from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.portfolio.provider import PortfolioProvider
from app.portfolio.service import PortfolioService
from app.shared.contracts import DataResult


def _json_result(result: DataResult[dict[str, object]]) -> JSONResponse:
    return JSONResponse(result.model_dump(mode="json", by_alias=True))


def create_router(service: PortfolioService | None = None, provider: PortfolioProvider | None = None) -> APIRouter:
    router = APIRouter()
    if service is None:
        if provider is None:
            raise ValueError("Portfolio routes require an explicit service or provider")
        service = PortfolioService(provider)
    active_service = service

    @router.get("/api/v1/portfolio")
    async def portfolio() -> JSONResponse:
        return _json_result(active_service.get_portfolio())

    @router.get("/api/data/portfolio")
    async def legacy_portfolio() -> JSONResponse:
        return _json_result(active_service.get_portfolio(legacy=True))

    return router
