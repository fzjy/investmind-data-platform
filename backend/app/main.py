from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.macro.database_provider import MacroDatabaseProvider
from app.macro.audit import CollectionAuditReader
from app.macro.routes import create_router as create_macro_router
from app.market.database_provider import MarketDatabaseProvider
from app.market.routes import create_router as create_market_router
from app.portfolio.database_provider import PortfolioDatabaseProvider
from app.portfolio.routes import create_router as create_portfolio_router
from app.shared.errors import DataServiceError, error_payload
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider
from app.stocks.routes import create_router as create_stocks_router
from app.search.routes import create_router as create_search_router
from app.search.models import SearchOptions
from app.content.routes import create_router as create_content_router
from app.content.news_worker import news_lifespan


def _request_id() -> str:
    return str(uuid.uuid4())


def _validation_details(exc: RequestValidationError) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    for item in exc.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "request"
        details.append({"field": location, "message": str(item.get("msg", "请求参数不合法"))})
    return details


def create_app(*, background_news: bool = True) -> FastAPI:
    application = FastAPI(title="DangInvest Data API", version="0.1.0", lifespan=news_lifespan if background_news else None)
    if not settings.database_read_url:
        raise ValueError("DANG_DATABASE_READ_URL or DANG_DATABASE_URL is required")
    engine_options: dict[str, object] = {"pool_pre_ping": True}
    if settings.database_read_url.startswith("sqlite"):
        engine_options["execution_options"] = {"schema_translate_map": {"raw": None, "core": None, "ops": None, "derived": None, "mart": None}}
    database_engine = create_engine(settings.database_read_url, **engine_options)
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

    session_factory = sessionmaker(database_engine, expire_on_commit=False)
    application.include_router(create_content_router(session_factory))
    repository = SnapshotRepository(session_factory)
    application.include_router(create_market_router(provider=MarketDatabaseProvider(repository)))
    application.include_router(create_macro_router(provider=MacroDatabaseProvider(repository), collection_audit=CollectionAuditReader(session_factory)))
    application.include_router(create_portfolio_router(provider=PortfolioDatabaseProvider(repository)))
    application.include_router(create_stocks_router(provider=StocksDatabaseProvider(repository, session_factory)))
    application.include_router(create_search_router(provider=StocksDatabaseProvider(repository),
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


app = create_app()
