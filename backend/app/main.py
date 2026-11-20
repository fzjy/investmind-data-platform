from __future__ import annotations

import os
import uuid
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from app.db.session import create_engine_and_session

from app.config import settings
from app.macro.database_provider import MacroDatabaseProvider
from app.macro.audit import CollectionAuditReader
from app.macro.routes import create_router as create_macro_router
from app.market.database_provider import MarketDatabaseProvider
from app.market.routes import create_router as create_market_router
from app.market.datasets_routes import create_router as create_market_datasets_router
from app.macro.datasets_routes import create_router as create_macro_datasets_router
from app.portfolio.database_provider import PortfolioDatabaseProvider
from app.portfolio.routes import create_router as create_portfolio_router
from app.shared.errors import DataServiceError, error_payload
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider
from app.stocks.datasets_routes import create_router as create_stock_datasets_router
from app.stocks.routes import create_router as create_stocks_router
from app.search.routes import create_router as create_search_router
from app.search.models import SearchOptions
from app.content.routes import create_router as create_content_router, read_news
from app.content.datasets_routes import create_router as create_content_datasets_router
from app.content.news_worker import news_lifespan


def _request_id() -> str:
    return str(uuid.uuid4())


def _validation_details(exc: RequestValidationError) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "request"
        details.append({"field": location, "message": str(item.get("msg", "请求参数不合法"))})
    return details


def create_app(*, background_news: bool = False, finance_mcp: bool | None = None, finance_only: bool = False) -> FastAPI:
    if finance_mcp is None:
        finance_mcp = os.environ.get("DANG_FINANCE_MCP_ENABLED", "").lower() in ("1", "true", "yes")
    transport = None

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            if background_news and not finance_only:
                await stack.enter_async_context(news_lifespan(app))
            if transport is not None:
                await stack.enter_async_context(transport.router.lifespan_context(transport))
            yield

    application = FastAPI(title="InvestMind Data API", version="0.1.0", lifespan=lifespan)
    if not settings.database_read_url:
        raise ValueError("DANG_DATABASE_READ_URL or DANG_DATABASE_URL is required")
    connection_options = {"options": "-c default_transaction_read_only=on -c statement_timeout=10000",
                          "connect_timeout": 5} if finance_only else None
    database_engine, session_factory = create_engine_and_session(settings.database_read_url, connection_options=connection_options)
    application.state.read_engine = database_engine
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["content-type"],
    )

    @application.get("/health", response_model=None)
    async def health() -> JSONResponse | dict[str, str]:
        try:
            with database_engine.connect() as connection:
                connection.execute(text("select 1"))
        except SQLAlchemyError:
            return JSONResponse(
                {"status": "degraded", "database": "unavailable"},
                status_code=503,
            )
        return {"status": "ok", "database": "ready"}

    from app.acquisition.routes import create_router as create_status_router
    from app.acquisition.service import StatusService
    application.state.acquisition_status = StatusService(session_factory)
    application.include_router(create_status_router(application.state.acquisition_status))
    application.include_router(create_content_router(session_factory))
    application.include_router(create_content_datasets_router(session_factory))
    application.include_router(create_market_datasets_router(session_factory))
    application.include_router(create_macro_datasets_router(session_factory))
    repository = SnapshotRepository(session_factory)
    application.include_router(create_market_router(
        provider=MarketDatabaseProvider(repository),
        latest_news=lambda: read_news(session_factory),
    ))
    application.include_router(create_macro_router(provider=MacroDatabaseProvider(repository), collection_audit=CollectionAuditReader(session_factory)))
    application.include_router(create_portfolio_router(provider=PortfolioDatabaseProvider(repository)))
    stocks = StocksDatabaseProvider(repository, session_factory)
    application.include_router(create_stocks_router(provider=stocks))
    application.include_router(create_stock_datasets_router(session_factory))
    search_provider = StocksDatabaseProvider(repository)
    search_options = SearchOptions.from_settings(settings)
    application.include_router(create_search_router(provider=search_provider, options=search_options))
    if finance_only or finance_mcp:
        from app.finance_mcp.server import attach
        transport = attach(application, only=finance_only, stocks=stocks, search_provider=search_provider,
                           options=search_options, repository=repository, status=application.state.acquisition_status,
                           sessions=session_factory)

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


app = create_app()
