"""将四个只读金融工具映射到现有服务，保留各自响应格式。"""
from __future__ import annotations

import json
from uuid import uuid4

from anyio import to_thread
from fastapi.encoders import jsonable_encoder
from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import QueryParams

from app.acquisition.contracts import AuditContext, QueryError, StatusQuery, UnknownResource
from app.acquisition.routes import envelope
from app.finance_mcp.evidence import evidence_catalog, expand_evidence
from app.search.routes import parse_search_query
from app.search.service import SearchService
from app.shared.contracts import success_result
from app.shared.errors import DataServiceError
from app.shared.validation import normalize_stock_symbol
from app.stocks.normalizer import normalize_workbench
from app.stocks.service import WorkbenchService


MAX_RESULT_BYTES = 4 * 1024 * 1024
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def tool_result(function, *args) -> CallToolResult:
    try:
        data = function(*args)
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) <= MAX_RESULT_BYTES:
            return CallToolResult(content=[TextContent(type="text", text=encoded)], structured_content=data)
        error = {"code": "RESULT_TOO_LARGE", "status": 413, "message": "查询结果超过体量上限"}
    except DataServiceError as exc:
        error = {"code": exc.code, "status": exc.status, "message": exc.message}
        if exc.details:
            error["details"] = exc.details
    except UnknownResource:
        error = {"code": "DATA_NOT_FOUND", "status": 404, "message": "目录、产品或版本不存在"}
    except QueryError as exc:
        error = {"code": "INVALID_REQUEST", "status": 422, "message": str(exc)}
    except SQLAlchemyError:
        error = {"code": "DATA_SOURCE_UNAVAILABLE", "status": 503, "message": "状态或快照数据库暂不可用"}
    data = {"error": error}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False))],
                          structured_content=data, is_error=True)


def filter_values(value: str | list[str] | None) -> tuple[str, ...]:
    values = [value] if isinstance(value, str) else value or []
    if len(values) > 100:
        raise QueryError("筛选条件过多")
    return tuple(dict.fromkeys(values))


def register_tools(server: MCPServer, *, stocks, search_provider, options, repository, status, sessions):
    search = SearchService(search_provider, options)
    workbench = WorkbenchService(stocks)

    def read_search(q, page, page_size):
        params = QueryParams({k: str(v) for k, v in {"q": q, "page": page, "pageSize": page_size}.items()
                              if v is not None})
        result = search.query(parse_search_query(params, options), request_id=str(uuid4()))
        return result.model_dump(mode="json", by_alias=True)

    def read_workbench(symbol, version, selected_metrics):
        symbol = normalize_stock_symbol(symbol)
        # 同一当前版本保留已发布的风险清单、新鲜度等字段；历史版本只使用指定快照。
        try:
            result = workbench.get_workbench(symbol)
        except DataServiceError as exc:
            if version is None or exc.code not in ("DATA_NOT_READY", "DATA_NOT_FOUND"):
                raise
            result = None
        if version is not None and (result is None or result.meta.version != version):
            historical = repository.read_versioned("stock-workbench:" + symbol, version)
            data = normalize_workbench(historical.raw, assess_risks=True)
            result = success_result({**data, "isFallbackSample": False}, historical.meta)
        payload = result.model_dump(mode="json", by_alias=True)
        if payload["data"].get("symbol") != symbol:
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "工作台证券与请求不一致")
        payload["evidenceRefs"] = evidence_catalog(symbol, result.meta.version, payload["data"], selected_metrics or [])
        return payload

    def read_status(entry, item, subject, subject_kind, scope, view, product, version, period, limit, cursor):
        query = StatusQuery(filter_values(entry), filter_values(item), subject_kind,
                            filter_values(subject), scope, AuditContext(view, product, version, period), limit, cursor)
        return jsonable_encoder(envelope(status.query(query)))

    @server.tool(annotations=READ_ONLY)
    async def search_securities(q: str, page: int | None = None, pageSize: int | None = None) -> CallToolResult:
        """按证券代码或名称搜索，保留证券搜索结果和分页元信息。"""
        return await to_thread.run_sync(tool_result, read_search, q, page, pageSize)

    @server.tool(annotations=READ_ONLY)
    async def get_stock_workbench(symbol: str, version: str | None = None,
                                  selectedMetrics: list[str] | None = None) -> CallToolResult:
        """读取所选证券的工作台。可指定快照版本和需要展开证据的规范指标 ID。"""
        return await to_thread.run_sync(tool_result, read_workbench, symbol, version, selectedMetrics)

    @server.tool(annotations=READ_ONLY)
    async def get_data_status(entryId: str | list[str] | None = None, itemId: str | list[str] | None = None,
                              subjectKey: str | list[str] | None = None, subjectKind: str | None = None,
                              scopeKind: str = "routine", view: str = "stored", product: str | None = None,
                              version: str | None = None, period: str | None = None, limit: int = 25,
                              cursor: str | None = None) -> CallToolResult:
        """按目录和对象读取采集、可用性与审计状态，保留数据库视图与发布视图区别。"""
        return await to_thread.run_sync(tool_result, read_status, entryId, itemId, subjectKey, subjectKind,
                                       scopeKind, view, product, version, period, limit, cursor)

    @server.tool(annotations=READ_ONLY)
    async def get_evidence(symbol: str, version: str, evidenceRef: str) -> CallToolResult:
        """按本轮所选证券、快照和已登记引用展开单项指标。"""
        return await to_thread.run_sync(tool_result, expand_evidence, sessions, symbol, version, evidenceRef)
