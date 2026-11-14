from __future__ import annotations
from typing import Literal
from collections.abc import Callable

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.macro.provider import MacroProvider
from app.macro.service import MacroService
from app.shared.contracts import DataResult


def _json_result(result: DataResult[dict[str, object]]) -> JSONResponse:
    return JSONResponse(result.model_dump(mode="json", by_alias=True))


def transmission_view(payload: dict, view: str) -> dict:
    if view != 'initial' or not isinstance(payload.get('enterpriseAnalysis'), dict):
        return payload
    return {**payload, 'enterpriseAnalysis': {key: value for key, value in payload['enterpriseAnalysis'].items()
        if key not in ('industryComparison', 'officialTargets')}}


def create_router(service: MacroService | None = None, provider: MacroProvider | None = None, *, collection_audit: Callable[[], dict | None] | None = None) -> APIRouter:
    router = APIRouter(prefix="/api/data")
    if service is None:
        if provider is None:
            raise ValueError("Macro routes require an explicit service or provider")
        service = MacroService(provider)
    active_service = service

    @router.get("/macro-transmission")
    def macro_transmission(version: str | None = Query(default=None, min_length=1, max_length=64),
                           view: Literal['full', 'initial'] = 'full') -> JSONResponse:
        result = active_service.get_transmission(version)
        return _json_result(result.model_copy(update={'data': transmission_view(result.data, view)}))

    @router.get("/macro-insights")
    def macro_insights(version: str | None = Query(default=None, min_length=1, max_length=64)) -> JSONResponse:
        result = active_service.get_insights(version)
        payload = result.model_dump(mode="json", by_alias=True)
        try:
            audit = collection_audit() if collection_audit else None
        except Exception:
            # 状态不可读不能破坏旧快照，也不透传内部异常。
            audit = None
        payload['collectionAudit'] = audit
        return JSONResponse(payload, headers={'Cache-Control': 'no-store'})

    return router
