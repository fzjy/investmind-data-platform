from __future__ import annotations

from app.macro.freshness import observation_expired
from app.macro.derivations import derive_macro_insights
from app.macro.state_rules import derive_macro_states

from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import IngestionBatch, MacroObservation, SourcePayload
from app.macro.dataset_query import (
    _released_at, select_catalog_observations, select_visible_observations, stored_observations,
)
from app.macro.import_lineage import is_verified_import
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSeriesDefinition
from app.marts.contracts import MartCandidate

if TYPE_CHECKING:
    from app.macro.jobs.macro_series_state import MacroSeriesStateRepository, MacroSeriesStateView
    from app.macro.jobs.macro_sync import MacroSeriesOutcome


CATEGORIES = ("growth", "inflation", "money_credit", "labor", "rates_fx")
COLORS = ("#38bdf8", "#a78bfa", "#fb7185", "#facc15", "#34d399")
WEATHER_IDS = frozenset({"cn.rate.dr007", "cn.rate.govbond_10y", "cn.fx.usdcny", "us.rate.effective_fed_funds", "us.rate.treasury_10y"})


def _as_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime.combine(value, time.max, UTC)


class MacroRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def visible_observations(
        self, series_ids: Sequence[str], as_of: datetime,
        source_priorities: Mapping[str, Sequence[str]] | None = None,
    ) -> tuple[MacroObservation, ...]:
        with self._session_factory() as session:
            rows = stored_observations(session, series_ids or None, as_of)
        if source_priorities is None:
            catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
            return select_catalog_observations(rows, catalog, as_of)
        return select_visible_observations(rows, as_of, source_priorities)

    def all_observations(self, series_ids: Sequence[str], as_of: datetime) -> tuple[MacroObservation, ...]:
        with self._session_factory() as session:
            return stored_observations(session, series_ids, as_of)

    def bls_retrieval_dates(self, latest: Mapping[str, MacroObservation], as_of: datetime) -> dict[str, datetime]:
        selected = {key: row for key, row in latest.items() if row.source == "bls"}
        if not selected:
            return {}
        dates: dict[str, datetime] = {}
        with self._session_factory() as session:
            payloads = session.scalars(select(SourcePayload).join(IngestionBatch).where(
                IngestionBatch.source == "bls", IngestionBatch.mode == "macro",
                IngestionBatch.dataset.in_([f"macro:{key}" for key in selected]),
                SourcePayload.captured_at <= as_of,
            ).order_by(SourcePayload.captured_at)).all()
            for payload in payloads:
                audit = payload.payload
                key = audit.get("seriesId")
                row = selected.get(key) if isinstance(key, str) else None
                data = audit.get("data")
                if row is None or audit.get("status") != "success" or not isinstance(data, Mapping):
                    continue
                normalized = data.get("normalizedObservations")
                if not isinstance(normalized, list):
                    continue
                for item in normalized:
                    if not isinstance(item, Mapping) or item.get("observedAt") != row.observed_at.isoformat() or item.get("sourceUrl") != row.source_url:
                        continue
                    value = item.get("value")
                    if not isinstance(value, str):
                        continue
                    try:
                        # Match the selected core value at its NUMERIC(28,10) storage precision.
                        number = Decimal(value)
                        matches = number.is_finite() and number.quantize(Decimal("0.0000000001")) == row.value
                    except InvalidOperation:
                        matches = False
                    if matches:
                        dates[row.series_id] = _released_at(payload.captured_at)
        return dates


def _iso(value: datetime | None) -> str | None:
    return _released_at(value).isoformat() if value is not None else None


def _period(row: MacroObservation) -> str:
    return row.observed_at.isoformat()


def _display(row: MacroObservation) -> str:
    return f"{row.value}{row.unit}"


def _change(rows: Sequence[MacroObservation]) -> str:
    if len(rows) < 2:
        return "--"
    change = rows[-1].value - rows[-2].value
    return f"{'+' if change > 0 else ''}{change}{rows[-1].unit}"


def series_status_payload(
    definition: MacroSeriesDefinition, state: MacroSeriesStateView | MacroSeriesOutcome | None,
    latest: MacroObservation | None,
    as_of: datetime,
    outcome: MacroSeriesOutcome | None = None,
) -> dict[str, object]:
    active_source = latest.source if latest is not None else getattr(state, "active_source", None)
    if active_source is None:
        active_source = getattr(outcome, "actual_source", None)
    fallback_used = bool(getattr(outcome if outcome is not None else state, "fallback_used", False))
    if latest is not None and definition.sources:
        fallback_used = fallback_used or latest.source != min(definition.sources, key=lambda source: source.priority).provider
    if latest is not None and is_verified_import(latest):
        fallback_used = True
    expired = latest is not None and observation_expired(definition, latest.observed_at, as_of.date())
    status_stale = (outcome.status in ("stale", "missing") if outcome is not None
                    else bool(getattr(state, "stale", False)))
    stale = status_stale or latest is None or expired
    stale_reason = None
    if latest is None:
        stale_reason = getattr(state, "stale_reason", None) or "missing"
    elif status_stale:
        stale_reason = getattr(outcome, "stale_reason", None) or getattr(state, "stale_reason", None) or "source_failed"
        if stale_reason == "missing":
            # 首次有观测但历史未齐时运维状态可能仍无 active_source；可见值并非缺失。
            stale_reason = getattr(state, "last_error_code", None) or "source_failed"
    elif expired:
        stale_reason = "MACRO_VALUE_EXPIRED"
    return {
        "id": definition.id, "name": definition.name, "region": definition.region,
        "category": definition.category, "frequency": definition.frequency, "unit": definition.unit,
        "actualSource": active_source, "sourceUrl": latest.source_url if latest else None,
        "fallbackUsed": fallback_used, "stale": stale,
        "staleReason": stale_reason,
        "observedAt": latest.observed_at.isoformat() if latest else None,
        "releasedAt": _iso(latest.released_at) if latest else None,
        "lastCheckedAt": _iso(getattr(state, "last_checked_at", None)),
        "lastSuccessAt": _iso(getattr(state, "last_success_at", None)),
    }


class MacroMartBuilder:
    def __init__(
        self, repository: MacroRepository, publisher: object | None = None, *,
        catalog: MacroSeriesCatalog | None = None,
        state_repository: MacroSeriesStateRepository | None = None,
        enterprise_loader: Callable[[date | datetime], dict] | None = None,
        extension_loader: Callable[[datetime], dict] | None = None,
    ) -> None:
        del publisher
        self._repository = repository
        self._catalog = catalog or MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
        self._state_repository = state_repository
        self._enterprise_loader = enterprise_loader
        self._extension_loader = extension_loader

    @property
    def extension_required(self) -> bool:
        return self._extension_loader is not None

    def load_extension_insights(self, as_of: date | datetime, observations: Sequence[MacroObservation]) -> dict | None:
        if self._extension_loader is None:
            return None
        from app.macro.extensions.view import build_extension_view
        cutoff = _as_datetime(as_of)
        selected = select_catalog_observations(observations, self._catalog, cutoff)
        real = [{'month': row.observed_at.isoformat(), 'value': str(row.value)} for row in selected
                if row.series_id == 'cn.gdp.real_yoy']
        return build_extension_view(self._extension_loader(cutoff), real)

    @property
    def enterprise_required(self) -> bool:
        return self._enterprise_loader is not None

    def load_enterprise_analysis(self, as_of: date | datetime) -> dict | None:
        return self._enterprise_loader(as_of) if self._enterprise_loader is not None else None

    def load_observations(self, catalog: MacroSeriesCatalog, as_of: datetime) -> tuple[MacroObservation, ...]:
        rows = self._repository.all_observations(tuple(item.id for item in catalog.series), as_of)
        return tuple(row for row in rows if _released_at(row.released_at) <= as_of)

    def build(
        self, as_of: date | datetime, version: str, series_ids: Sequence[str] = (),
        observations: Sequence[MacroObservation] | None = None, *,
        catalog: MacroSeriesCatalog | None = None,
        states: Sequence[MacroSeriesStateView | MacroSeriesOutcome] | None = None,
        enterprise_analysis: dict | None = None,
        extension_insights: dict | None = None,
    ) -> tuple[MartCandidate, MartCandidate]:
        del version
        cutoff = _as_datetime(as_of)
        candidate_as_of = cutoff if isinstance(as_of, datetime) else datetime.combine(as_of, time.min, UTC)
        definitions = tuple(item for item in (catalog or self._catalog).series if not series_ids or item.id in series_ids)
        ids = tuple(item.id for item in definitions)
        priorities = {
            item.id: tuple(source.provider for source in sorted(item.sources, key=lambda source: source.priority))
            for item in definitions
        }
        if observations is None:
            rows = self._repository.visible_observations(ids, cutoff, priorities)
        else:
            rows = select_visible_observations(tuple(row for row in observations if row.series_id in ids), cutoff, priorities)
        stored_states = self._state_repository.get_all() if self._state_repository is not None else ()
        state_rows = (*stored_states, *(item for item in (states or ()) if not hasattr(item, "status")))
        outcomes = {item.series_id: item for item in (states or ()) if hasattr(item, "status")}
        state_by_id = {
            item.series_id: item for item in state_rows
            if getattr(item, "last_checked_at", None) is None
            or _released_at(getattr(item, "last_checked_at")) <= cutoff
        }
        by_series: dict[str, list[MacroObservation]] = {item.id: [] for item in definitions}
        for row in rows:
            by_series[row.series_id].append(row)
        for series_rows in by_series.values():
            series_rows.sort(key=lambda row: (row.observed_at, _released_at(row.released_at)))
        latest = {series_id: series_rows[-1] for series_id, series_rows in by_series.items() if series_rows}
        retrieved = self._repository.bls_retrieval_dates(latest, cutoff) if any(row.source == "bls" for row in latest.values()) else {}
        statuses = [series_status_payload(item, state_by_id.get(item.id), latest.get(item.id), cutoff, outcomes.get(item.id)) for item in definitions]
        for status in statuses:
            if status["actualSource"] == "bls":
                status["retrievedAt"] = _iso(retrieved.get(str(status["id"])))
        missing = [{"source": item.id, "reason": "no-visible-observation"} for item in definitions if item.id not in latest]
        stale_count = sum(bool(item["stale"]) for item in statuses)
        stale = stale_count > 0
        stale_reason = f"stale={stale_count};missing={len(missing)}" if stale else None

        def points(series_id: str, key: str) -> list[dict[str, object]]:
            return [{key: _period(row), "value": float(row.value)} for row in by_series[series_id]]

        derived = derive_macro_insights({key: points(key, 'month') for key in by_series}, statuses)
        derived_by_id = {item['id']: item for item in derived['series']}

        def node(item: MacroSeriesDefinition) -> dict[str, object]:
            series_rows = by_series[item.id]
            current = series_rows[-1]
            return {"id": item.id, "title": item.name, "state": "open", "metric": item.name,
                    "value": _display(current), "change": _change(series_rows),
                    "series": points(item.id, "quarter")}

        modes = []
        for category in CATEGORIES:
            members = [item for item in definitions if item.category == category and item.id in latest]
            modes.append({"id": category, "label": category,
                          "targets": [{"label": item.name, "value": item.id} for item in members],
                          "mainNodes": [node(item) for item in members], "branches": []})
        cards = []
        for card_id, first, second, title in (
            ("cn.ppi_cpi_spread", "cn.ppi.yoy", "cn.cpi.yoy", "PPI-CPI 剪刀差"),
            ("cn.m2_m1_spread", "cn.money.m2_yoy", "cn.money.m1_yoy", "M2-M1 剪刀差"),
            ("cn.govbond_dr007_spread", "cn.rate.govbond_10y", "cn.rate.dr007", "国债-DR007 利差"),
            ("us.treasury_fed_funds_spread", "us.rate.treasury_10y", "us.rate.effective_fed_funds", "美债-联邦基金利差"),
        ):
            if first not in latest or second not in latest:
                missing.extend({"source": series_id, "reason": "derived-input-missing", "card": card_id}
                               for series_id in (first, second) if series_id not in latest)
            elif latest[first].observed_at != latest[second].observed_at:
                missing.extend({"source": series_id, "reason": "derived-period-mismatch", "card": card_id}
                               for series_id in (first, second))
            result = derived_by_id[card_id]
            if result['points']:
                first_history = {row.observed_at.isoformat(): row for row in by_series[first]}
                second_history = {row.observed_at.isoformat(): row for row in by_series[second]}
                cards.append({"id": card_id, "title": title, "defaultPeriod": 8,
                              "description": "同期间可见历史差值；最新输入未对齐或过期时保留缺口，不代表最新时点完整",
                              "meta": {"first": first, "second": second,
                                       "frequency": result['frequency'], "resultUnit": result['unit'],
                                       "latestPeriod": result['latestPeriod'], "status": result['status'],
                                       "reason": result['reason'],
                                       "primaryLabel": self._catalog.require(first).name,
                                       "secondaryLabel": self._catalog.require(second).name,
                                       "resultLabel": title, "primaryColor": COLORS[0], "secondaryColor": COLORS[2]},
                              "points": [{"month": point['month'], "quarter": point['month'],
                                          "primary": float(first_history[point['month']].value),
                                          "secondary": float(second_history[point['month']].value),
                                          "result": point['value'], "value": point['value']}
                                         for point in result['points']]})
        transmission: dict[str, object] = {
            "transmission": {"weather": [
                {"code": item.id, "value": _display(latest[item.id]), "change": _change(by_series[item.id])}
                for item in definitions if item.id in WEATHER_IDS and item.id in latest
            ], "modes": modes, "industries": []},
            "analysis": {"cards": cards}, "seriesStatus": [dict(item) for item in statuses], "missingData": list(missing),
        }
        if enterprise_analysis is not None:
            transmission['enterpriseAnalysis'] = deepcopy(enterprise_analysis)
        groups = [{"id": category, "label": category,
                   "indicatorIds": [item.id for item in definitions if item.category == category]}
                  for category in CATEGORIES]
        trends = [{"auxiliarySeriesIds": [], "code": item.id,
                   "description": f"{item.region} · {item.frequency}", "formula": "原始观测值",
                   "id": item.id, "primarySeriesIds": [item.id],
                   "series": [{"color": COLORS[index % len(COLORS)], "id": item.id,
                               "label": item.name, "values": points(item.id, "month")}], "title": item.name}
                  for index, item in enumerate(definitions)]
        indicators = [{"axis": "left", "color": COLORS[index % len(COLORS)],
                       "colorClass": "text-sky-400", "groupId": item.category,
                       "id": item.id, "label": item.name, "unit": item.unit,
                       "values": points(item.id, "month")}
                      for index, item in enumerate(definitions)]
        insights: dict[str, object] = {
            "compassPoints": derived['compass']['points'], "derivedInsights": derived,
            "stateInsights": derive_macro_states({key: points(key, 'month') for key in by_series}, statuses),
            "sandboxGroups": groups, "sandboxIndicators": indicators,
            "sandboxMonths": sorted({_period(row) for row in rows}),
            "sandboxPresets": [{"expandedGroupId": category, "id": category,
                                "indicatorIds": group["indicatorIds"], "label": category}
                               for category, group in zip(CATEGORIES, groups)],
            "statusCards": [{"detail": _display(latest[item.id]), "id": item.id,
                             "label": item.name, "status": "最新可见", "tone": "cyan"}
                            for item in definitions if item.id in latest],
            "trendDefinitions": trends, "quality": "partial" if stale else "ok",
            "missingSeries": [item.id for item in definitions if item.id not in latest],
            "seriesStatus": [dict(item) for item in statuses], "missingData": list(missing),
        }
        if extension_insights is not None:
            transmission['extensionInsights'] = deepcopy(extension_insights)
            insights['extensionInsights'] = deepcopy(extension_insights)
        return (
            MartCandidate("macro-transmission", candidate_as_of, transmission, missing_data=tuple(missing),
                          stale=stale, stale_reason=stale_reason),
            MartCandidate("macro-insights", candidate_as_of, insights, missing_data=tuple(missing),
                          stale=stale, stale_reason=stale_reason),
        )
