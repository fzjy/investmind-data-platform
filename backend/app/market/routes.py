from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.market.provider import MarketProvider
from app.market.service import MarketService
from app.shared.contracts import DataResult


def _json_result(result: DataResult[dict[str, object]]) -> JSONResponse:
    return JSONResponse(result.model_dump(mode="json", by_alias=True))


def create_router(service: MarketService | None = None, provider: MarketProvider | None = None) -> APIRouter:
    router = APIRouter()
    if service is None:
        if provider is None:
            raise ValueError("Market routes require an explicit service or provider")
        service = MarketService(provider)
    active_service = service

    @router.get("/api/data/market-overview")
    @router.get("/api/v1/market/overview")
    async def market_overview() -> JSONResponse:
        return _json_result(active_service.get_overview())

    @router.get("/api/data/market-explorer")
    @router.get("/api/v1/market/explorer")
    async def market_explorer() -> JSONResponse:
        return _json_result(active_service.get_explorer())

    return router
