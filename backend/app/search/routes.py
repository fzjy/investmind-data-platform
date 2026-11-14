import re
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import QueryParams

from app.config import settings
from app.search.matching import normalize_query
from app.search.models import SearchOptions, SearchRequest
from app.search.service import SearchService
from app.shared.errors import DataServiceError
from app.stocks.api.quotes import StocksProvider


def parse_search_query(params: QueryParams, options: SearchOptions) -> SearchRequest:
    if set(params) - {"q", "page", "pageSize"} or any(len(params.getlist(key)) != 1 for key in params):
        raise DataServiceError("INVALID_REQUEST", "搜索参数不合法或重复")
    values: dict[str, object] = {"q": params.get("q", "")}
    for key in ("page", "pageSize"):
        if key in params:
            value = params[key]
            # 统一JS安全整数边界，避免Python巨大整数与浏览器分页语义分歧。
            if not re.fullmatch(r"[0-9]{1,16}", value) or not 1 <= int(value) <= 9007199254740991:
                raise DataServiceError("INVALID_REQUEST", "搜索分页参数不合法")
            values[key] = int(value)
    try:
        request = SearchRequest.model_validate(values)
    except ValidationError:
        raise DataServiceError("INVALID_REQUEST", "搜索参数不合法") from None
    normalize_query(request.q, options)
    if request.page_size is not None and request.page_size > options.max_page_size:
        raise DataServiceError("INVALID_REQUEST", "搜索页大小超出上限")
    return request


def create_router(provider: StocksProvider, options: SearchOptions | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/data")
    active_options = options or SearchOptions.from_settings(settings)
    service = SearchService(provider, active_options)

    @router.get("/search")
    async def search(request: Request) -> JSONResponse:
        result = service.query(parse_search_query(request.query_params, active_options), request_id=str(uuid.uuid4()))
        return JSONResponse(result.model_dump(mode="json", by_alias=True))

    return router
