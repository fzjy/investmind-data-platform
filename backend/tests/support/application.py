"""显式隔离测试应用；不提供生产运行入口。"""
from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from backend.tests.support.fixtures import FixtureStore
from backend.tests.support.providers import MacroFixtureProvider
from app.macro.routes import create_router as create_macro_router
from backend.tests.support.providers import MarketFixtureProvider
from app.market.routes import create_router as create_market_router
from backend.tests.support.providers import PortfolioFixtureProvider
from app.portfolio.routes import create_router as create_portfolio_router
from app.shared.errors import DataServiceError, error_payload
from backend.tests.support.providers import StocksFixtureProvider
from app.stocks.routes import create_router as create_stocks_router
from app.search.routes import create_router as create_search_router
from app.search.models import SearchOptions


def _request_id() -> str:
    return str(uuid.uuid4())


def _validation_details(exc: RequestValidationError) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "request"
        details.append({"field": location, "message": str(item.get("msg", "请求参数不合法"))})
    return details


def create_app() -> FastAPI:
    application = FastAPI(title="InvestMind Data API", version="0.1.0")
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type"],
    )

    @application.get("/health", response_model=None)
    async def health() -> JSONResponse | dict[str, str]:
        return {"status": "ok"}

    store = FixtureStore.default()
    application.include_router(create_market_router(provider=MarketFixtureProvider(store)))
    application.include_router(create_macro_router(provider=MacroFixtureProvider(store)))
    application.include_router(create_portfolio_router(provider=PortfolioFixtureProvider(store)))
    application.include_router(create_stocks_router(provider=StocksFixtureProvider(store)))
    application.include_router(create_search_router(provider=StocksFixtureProvider(store),
        options=SearchOptions.from_settings(settings)))

    @application.exception_handler(DataServiceError)
    async def handle_service_error(_request: Request, exc: DataServiceError) -> JSONResponse:
        request_id = _request_id()
        return JSONResponse(error_payload(exc, request_id), status_code=exc.status)

    @application.exception_handler(SQLAlchemyError)
    async def handle_database_error(_request: Request, _exc: SQLAlchemyError) -> JSONResponse:
        error = DataServiceError("DATA_SOURCE_UNAVAILABLE", "数据库暂不可用")
        return JSONResponse(error_payload(error, _request_id()), status_code=503)

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        error = DataServiceError(
            "INVALID_REQUEST",
            "请求参数不合法",
            _validation_details(exc),
        )
        return JSONResponse(error_payload(error, _request_id()), status_code=400)

    @application.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        error = DataServiceError("INTERNAL_ERROR", "服务端处理失败")
        return JSONResponse(error_payload(error, _request_id()), status_code=500)

    return application
