from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.shared.contracts import DataResult
from app.stocks.models import ScreenerRequest
from app.stocks.provider import StocksProvider
from app.stocks.service import ScreenerService, WorkbenchService
from app.stocks.research_tools import ResearchRequest, ResearchToolsService


def _json_result(result: DataResult[dict[str, object]]) -> JSONResponse:
    return JSONResponse(result.model_dump(mode="json", by_alias=True))


def create_router(service: ScreenerService | None = None, provider: StocksProvider | None = None) -> APIRouter:
    router = APIRouter()
    if provider is None:
        raise ValueError("Stocks routes require an explicit provider")
    active_provider = provider
    active_service = service or ScreenerService(active_provider)
    workbench_service = WorkbenchService(active_provider)

    @router.post("/api/data/stocks/research")
    @router.post("/api/v1/stocks/research")
    def research_tools(request: ResearchRequest) -> JSONResponse:
        return _json_result(ResearchToolsService(active_provider).query(request))

    @router.post("/api/data/screener")
    @router.post("/api/v1/stocks/screener")
    async def screener(request: ScreenerRequest) -> JSONResponse:
        return _json_result(active_service.query(request))

    @router.get("/api/data/stocks/{symbol}")
    @router.get("/api/v1/stocks/{symbol}")
    async def stock_workbench(symbol: str) -> JSONResponse:
        return _json_result(workbench_service.get_workbench(symbol))

    return router
