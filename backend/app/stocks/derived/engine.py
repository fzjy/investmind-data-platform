from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import MetricInput, MetricValue, Security
from app.stocks.derived.registry import FORMULAS, calculate_formula
from app.stocks.derived.inputs import InputSource


@dataclass(frozen=True)
class MetricResultRecord:
    ts_code: str
    metric_id: str
    as_of: date
    value: Decimal | None
    unit: str | None
    formula_version: str
    quality: str
    missing_fields: tuple[str, ...]


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class MetricEngine:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def calculate_inputs(
        self,
        ts_code: str,
        as_of: date,
        inputs: Mapping[str, Any],
        metric_ids: tuple[str, ...] | None = None,
        sources: Mapping[str, InputSource] | None = None,
    ) -> tuple[MetricResultRecord, ...]:
        selected = metric_ids or tuple(FORMULAS)
        records: list[MetricResultRecord] = []
        source_map = sources or {}
        with self._session_factory.begin() as session:
            security = session.get(Security, ts_code)
            if security is None:
                raise ValueError(f"unknown security: {ts_code}")
            for metric_id in selected:
                definition = FORMULAS[metric_id]
                result = calculate_formula(metric_id, inputs)
                record = MetricResultRecord(ts_code, metric_id, as_of, result.value, result.unit, definition.version, result.quality, result.missing)
                existing = session.scalar(select(MetricValue).where(MetricValue.ts_code == ts_code, MetricValue.metric_id == metric_id, MetricValue.as_of == as_of, MetricValue.formula_version == definition.version))
                if existing is None:
                    existing = MetricValue(ts_code=ts_code, metric_id=metric_id, as_of=as_of, value=result.value, unit=result.unit, formula_version=definition.version, quality=result.quality, missing_fields=list(result.missing))
                    session.add(existing)
                    session.flush()
                else:
                    existing.value = result.value
                    existing.unit = result.unit
                    existing.quality = result.quality
                    existing.missing_fields = list(result.missing)
                session.execute(delete(MetricInput).where(MetricInput.metric_value_id == existing.id))
                for input_name in definition.inputs:
                    source = source_map.get(
                        input_name,
                        InputSource(
                            source_table="input_context",
                            source_record_key=f"{ts_code}:{as_of.isoformat()}:{input_name}",
                            value=inputs.get(input_name),
                        ),
                    )
                    session.add(
                        MetricInput(
                            metric_value_id=existing.id,
                            input_name=input_name,
                            source_table=source.source_table,
                            source_record_key=source.source_record_key,
                            content_hash=source.content_hash,
                        )
                    )
                records.append(record)
        return tuple(records)

    def calculate_security(self, ts_code: str, as_of: date, inputs: Mapping[str, Any] | None = None, metric_ids: tuple[str, ...] | None = None) -> tuple[MetricResultRecord, ...]:
        """计算一个证券的指标；生产任务传入已按公告可见性整理好的输入快照。"""
        return self.calculate_inputs(ts_code, as_of, inputs or {}, metric_ids)


def persist_metric_cards(session: Session, inputs: dict) -> tuple[MetricValue, ...]:
    """在调用方持有的发布事务内保存一只证券的全部八卡。"""
    from app.stocks.derived.workbench_cards import CARD_IDS, CARD_VERSION, DEFINITIONS, calculate_metric_cards

    ts_code = inputs["symbol"]
    as_of = date.fromisoformat(inputs["asOf"])
    current = {row.metric_id: row for row in session.scalars(select(MetricValue).where(
        MetricValue.ts_code == ts_code, MetricValue.as_of == as_of,
        MetricValue.formula_version == CARD_VERSION, MetricValue.metric_id.in_(CARD_IDS)))}
    if current:
        session.execute(delete(MetricInput).where(MetricInput.metric_value_id.in_([r.id for r in current.values()])))
    records = []
    evidence = []
    for card in calculate_metric_cards(inputs):
        record = current.get(card.metric_id)
        if record is None:
            record = MetricValue(ts_code=ts_code, as_of=as_of, metric_id=card.metric_id, formula_version=CARD_VERSION)
            session.add(record)
        record.value = card.value
        record.unit = DEFINITIONS[card.metric_id][1]
        record.quality = "missing" if card.missing else "ok"
        record.missing_fields = card.missing
        record.calculation = card.calculation()
        record.calculated_at = datetime.now(timezone.utc)
        records.append(record)
        evidence.append(card.sources)
    session.flush()
    for record, sources in zip(records, evidence, strict=True):
        for key, entries in sources.items():
            table, _, record_key = key.partition(":")
            source = InputSource(table, record_key, entries)
            session.add(MetricInput(metric_value_id=record.id, input_name="source", source_table=table,
                                    source_record_key=record_key, content_hash=source.content_hash))
        # 缺行也是输入状态。将缺项、排除项和公式版本一起纳入签名。
        result_source = InputSource("derived.workbench_calculation", record.metric_id, record.calculation)
        session.add(MetricInput(metric_value_id=record.id, input_name="calculation", source_table=result_source.source_table,
                                source_record_key=result_source.source_record_key, content_hash=result_source.content_hash))
    session.flush()
    return tuple(records)
