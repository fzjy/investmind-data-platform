from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Mapping

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import MetricInput, MetricValue
from app.stocks.derived.engine import MetricEngine, MetricResultRecord, persist_metric_cards
from app.stocks.derived.inputs import CoreSnapshot, InputSource
from app.shared.fingerprints import canonical_source_hash
from app.stocks.derived.registry import FORMULAS


@dataclass(frozen=True)
class DerivedReport:
    symbols: tuple[str, ...]
    metric_count: int
    risk_count: int
    declared_lineage_count: int
    persisted_lineage_count: int
    lineage_errors: tuple[str, ...] = ()


class DerivedPipeline:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._engine = MetricEngine(session_factory)

    @staticmethod
    def run_metric_cards(session: Session, inputs_by_symbol: Mapping[str, dict]) -> dict[str, tuple[MetricValue, ...]]:
        return {symbol: persist_metric_cards(session, inputs) for symbol, inputs in inputs_by_symbol.items()}

    def run(
        self,
        snapshot: CoreSnapshot,
        symbols: tuple[str, ...] = (),
    ) -> DerivedReport:
        selected = tuple(symbols) or tuple(snapshot.security_inputs)
        metric_count = 0
        risk_count = 0
        records: list[MetricResultRecord] = []
        for ts_code in selected:
            context = snapshot.security_inputs.get(ts_code)
            if context is None:
                continue
            calculated = self._engine.calculate_inputs(
                ts_code,
                snapshot.as_of,
                context.values,
                sources=context.sources,
            )
            records.extend(calculated)
            metric_count += len(calculated)
        declared = sum(
            len(FORMULAS[record.metric_id].inputs) for record in records
        )
        persisted = self._lineage_count(selected, snapshot.as_of)
        return DerivedReport(
            symbols=selected,
            metric_count=metric_count,
            risk_count=risk_count,
            declared_lineage_count=declared,
            persisted_lineage_count=persisted,
            lineage_errors=self._lineage_errors(snapshot, selected),
        )

    def _lineage_errors(self, snapshot: CoreSnapshot, symbols: tuple[str, ...]) -> tuple[str, ...]:
        expected = {}
        expected_metrics = set()
        for symbol in symbols:
            context = snapshot.security_inputs.get(symbol)
            if context is None:
                continue
            for metric_id, definition in FORMULAS.items():
                expected_metrics.add((symbol, metric_id))
                for name in definition.inputs:
                    source = context.sources.get(name, InputSource(
                        "input_context", f"{symbol}:{snapshot.as_of.isoformat()}:{name}", context.values.get(name)))
                    expected[(symbol, metric_id, name)] = (
                        source.source_table, source.source_record_key,
                        canonical_source_hash(context.values.get(name), source.source_table, source.source_record_key))
        if not symbols:
            return ()
        with self._session_factory() as session:
            records = session.execute(select(MetricValue, MetricInput).outerjoin(
                MetricInput, MetricInput.metric_value_id == MetricValue.id).where(
                    MetricValue.ts_code.in_(symbols), MetricValue.as_of == snapshot.as_of,
                    tuple_(MetricValue.metric_id, MetricValue.formula_version).in_(
                        [(item.metric_id, item.version) for item in FORMULAS.values()]))).all()
        seen, seen_metrics, errors = set(), set(), set()
        for metric, row in records:
            seen_metrics.add((metric.ts_code, metric.metric_id))
            if row is None:
                continue
            key = (metric.ts_code, metric.metric_id, row.input_name)
            if key in seen or key not in expected:
                errors.add(":".join((*key, "unexpected-or-duplicate")))
            elif (row.source_table, row.source_record_key, row.content_hash) != expected[key]:
                errors.add(":".join((*key, "source-identity-or-hash")))
            seen.add(key)
        errors.update(":".join((*key, "missing-input")) for key in expected.keys() - seen)
        errors.update(":".join((*key, "missing-metric")) for key in expected_metrics - seen_metrics)
        return tuple(sorted(errors))

    def _lineage_count(
        self,
        symbols: tuple[str, ...],
        as_of: date,
    ) -> int:
        if not symbols:
            return 0
        with self._session_factory() as session:
            return int(
                session.scalar(
                    select(func.count(MetricInput.id))
                    .join(
                        MetricValue,
                        MetricInput.metric_value_id == MetricValue.id,
                    )
                    .where(
                        MetricValue.ts_code.in_(symbols),
                        MetricValue.as_of == as_of,
                        tuple_(MetricValue.metric_id, MetricValue.formula_version).in_(
                            [(definition.metric_id, definition.version) for definition in FORMULAS.values()]
                        ),
                    )
                )
                or 0
            )


__all__ = ["DerivedPipeline", "DerivedReport"]
