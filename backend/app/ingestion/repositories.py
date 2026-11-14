from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.tushare.contracts import TushareResponse, request_hash
from app.db.models import CashflowStatement, DailyBasic, FinancialForecast, FinancialIndicator, IncomeStatement, Security, IngestionBatch, RejectedRecord, SourcePayload


ModelT = TypeVar("ModelT")


def content_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return sha256(encoded).hexdigest()


class IngestionRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def start_batch(self, dataset: str, mode: str | None) -> IngestionBatch:
        batch = IngestionBatch(source="tushare", dataset=dataset, mode=mode)
        self.session.add(batch)
        self.session.flush()
        return batch

    def save_payload(
        self,
        batch: IngestionBatch,
        api_name: str,
        params: Mapping[str, object],
        fields: Sequence[str],
        response: TushareResponse,
    ) -> SourcePayload:
        raw = {"fields": list(response.fields), "items": [list(item) for item in response.items]}
        request_key = request_hash(api_name, params, fields)
        response_hash = content_hash(raw)
        payload = SourcePayload(
            batch_id=batch.id,
            api_name=api_name,
            request_hash=request_key,
            content_hash=response_hash,
            payload=raw,
        )
        self.session.add(payload)
        self.session.flush()
        return payload

    def reject(
        self,
        batch: IngestionBatch,
        reason: str,
        *,
        row_number: int | None = None,
        error_path: str | None = None,
        raw_record: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            RejectedRecord(
                batch_id=batch.id,
                row_number=row_number,
                error_path=error_path,
                reason=reason,
                raw_record=raw_record,
            )
        )

    def upsert(self, model: type[ModelT], keys: Mapping[str, object], values: Mapping[str, object]) -> ModelT:
        statement = select(model).filter_by(**dict(keys))
        if model in (DailyBasic, Security, FinancialForecast, FinancialIndicator, IncomeStatement, CashflowStatement):
            statement = statement.with_for_update().execution_options(populate_existing=True)
        instance = self.session.scalar(statement)
        if instance is None:
            attributes = dict(keys)
            attributes.update(values)
            instance = model(**attributes)  # type: ignore[call-arg]
            self.session.add(instance)
        else:
            same_cashflow_revision = (model is CashflowStatement
                and instance.f_ann_date == values.get("f_ann_date")
                and instance.comp_type == values.get("comp_type"))
            for key, value in values.items():
                if key == "data" and (model in (IncomeStatement, FinancialIndicator) or same_cashflow_revision):
                    # 同一版本允许窄字段合同重放；只覆盖本次接口实际返回的键。
                    value = (instance.data or {}) | value
                if model is FinancialForecast and key == "data":
                    # 旧字段合同重读同一预告时保留详情；基础值修订后重新核对。
                    old = instance.data or {}
                    extra = ("first_ann_date", "summary", "change_reason")
                    if all(old.get(k) == v for k, v in value.items() if k not in extra):
                        value = {**{k: old[k] for k in extra if k in old}, **value}
                if model is DailyBasic and key == 'data' and '_limitStatusLineage' in (instance.data or {}):
                    # 独立字段所有权：所有普通入库入口不能覆盖脉搏组状态/来源。
                    value = {**value, **{name: instance.data.get(name)
                        for name in ('limit_status', '_limitStatusLineage')}}
                if model is Security and key == 'data':
                    # 无标记遗留值也不能被省略地域的旧字段合同清空；冲突证据同样保留。
                    value = {**value, **{name: instance.data[name] for name in ('area', 'region', '_regionLineage')
                        if name in (instance.data or {})}}
                setattr(instance, key, value)
        self.session.flush()
        return instance
