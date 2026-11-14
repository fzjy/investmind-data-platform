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
    enterprise = payload.get('enterpriseAnalysis')
    deferred = ('industryComparison', 'officialTargets')
    if view == 'scope':
        return {'enterpriseAnalysis': {key: enterprise[key] for key in ('inputHash', *deferred)
                                      if key in enterprise} if isinstance(enterprise, dict) else None}
    if view == 'initial' and isinstance(enterprise, dict):
        return {**payload, 'enterpriseAnalysis': {key: value for key, value in enterprise.items() if key not in deferred}}
    return payload


def create_router(service: MacroService | None = None, provider: MacroProvider | None = None, *, collection_audit: Callable[[], dict | None] | None = None) -> APIRouter:
    router = APIRouter()
    if service is None:
        if provider is None:
            raise ValueError("Macro routes require an explicit service or provider")
        service = MacroService(provider)
    active_service = service

    @router.get("/api/data/macro-transmission")
    @router.get("/api/v1/macro/transmission")
    def macro_transmission(version: str | None = Query(default=None, min_length=1, max_length=64),
                           view: Literal['full', 'initial', 'scope'] = 'full') -> JSONResponse:
        result = active_service.get_transmission(version)
        return _json_result(result.model_copy(update={'data': transmission_view(result.data, view)}))

    @router.get("/api/data/macro-insights")
    @router.get("/api/v1/macro/insights")
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
