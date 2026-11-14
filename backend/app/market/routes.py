from __future__ import annotations

from collections.abc import Callable
from typing import Literal
from sqlalchemy.exc import SQLAlchemyError
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.market.provider import MarketProvider
from app.market.service import MarketService
from app.shared.contracts import DataResult


def _json_result(result: DataResult[dict[str, object]]) -> JSONResponse:
    return JSONResponse(result.model_dump(mode="json", by_alias=True))


def create_router(service: MarketService | None = None, provider: MarketProvider | None = None,
                  *, latest_news: Callable[[], dict] | None = None) -> APIRouter:
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
    def market_explorer(news: Literal['snapshot', 'latest'] = 'snapshot') -> JSONResponse:
        result = active_service.get_explorer()
        if news == 'latest' and latest_news is not None:
            try:
                page = latest_news()
            except SQLAlchemyError:
                # 市场主体仍可展示；客户端沿既有快讯接口重试。
                return _json_result(result)
            items, pagination = page['items'], {key: page[key] for key in ('health', 'nextCursor')}
            data = {**result.data, 'marketNews': items, 'liveNewsPage': pagination}
            return _json_result(result.model_copy(update={'data': data}))
        return _json_result(result)

    return router
