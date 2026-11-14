from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.connectors.macro.contracts import MacroFetchWindow, ProviderObservation, ProviderSchemaError
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


def level_window_start(series: MacroSeriesDefinition, source: MacroSourceDefinition, start: date) -> date:
    if source.transform == "identity":
        return start
    if series.frequency != "monthly" or source.transform not in {"pc1", "chg"}:
        raise ProviderSchemaError("FRED_TRANSFORM_UNSUPPORTED")
    months = 12 if source.transform == "pc1" else 1
    ordinal = start.year * 12 + start.month - 1 - months
    return date(ordinal // 12, ordinal % 12 + 1, 1)


@dataclass(frozen=True, slots=True)
class _Level:
    start: date
    end: date
    value: Decimal | None


def _date(value: object) -> date:
    if not isinstance(value, str):
        raise ProviderSchemaError("FRED_REALTIME_INVALID")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ProviderSchemaError("FRED_REALTIME_INVALID") from exc


def transformed_observations(
    series: MacroSeriesDefinition, source: MacroSourceDefinition,
    payload: Mapping[str, object], window: MacroFetchWindow,
) -> tuple[ProviderObservation, ...]:
    """在分子与基期的共同有效 vintage 内计算，基期修订也生成新版本。"""
    rows = payload.get("observations")
    if not isinstance(rows, list):
        raise ProviderSchemaError("FRED_OBSERVATIONS_MISSING")
    first = level_window_start(series, source, window.start)
    periods: dict[date, set[_Level]] = {}
    cutoff = window.as_of.astimezone(UTC).date()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ProviderSchemaError("FRED_OBSERVATION_INVALID")
        period = _date(row.get("date"))
        if not first <= period <= min(window.end, cutoff):
            continue
        start, end = _date(row.get("realtime_start")), _date(row.get("realtime_end"))
        if start > end or period.day != 1:
            raise ProviderSchemaError("FRED_REALTIME_INVALID")
        if start > cutoff:
            continue
        raw = row.get("value")
        if raw is None or raw == ".":
            value = None
        else:
            if not isinstance(raw, str):
                raise ProviderSchemaError("FRED_VALUE_INVALID")
            try:
                value = Decimal(raw)
            except InvalidOperation as exc:
                raise ProviderSchemaError("FRED_VALUE_INVALID") from exc
            if not value.is_finite():
                raise ProviderSchemaError("FRED_VALUE_INVALID")
        periods.setdefault(period, set()).add(_Level(start, end, value))
    ordered = {period: sorted(levels, key=lambda level: level.start) for period, levels in periods.items()}
    for levels in ordered.values():
        if any(left.end >= right.start for left, right in zip(levels, levels[1:])):
            raise ProviderSchemaError("FRED_REALTIME_CONFLICT")
    observations = []
    for period, levels in sorted(ordered.items()):
        if not window.start <= period <= window.end:
            continue
        base_period = level_window_start(series, source, period)
        bases = ordered.get(base_period, [])
        events = sorted({level.start for level in (*levels, *bases)})
        for event in events:
            current = next((level for level in levels if level.start <= event <= level.end), None)
            base = next((level for level in bases if level.start <= event <= level.end), None)
            if current is None or base is None or current.value is None or base.value is None:
                continue
            if source.transform == "pc1":
                if base.value == 0:
                    raise ProviderSchemaError("FRED_TRANSFORM_ZERO_BASE")
                value = (current.value / base.value - Decimal(1)) * Decimal(100)
            else:
                value = current.value - base.value
            # core.macro_observation uses NUMERIC(28,10). Keep provider revisions
            # stable across first storage and replay; never round the stored row.
            try:
                value = value.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP)
            except InvalidOperation as exc:
                raise ProviderSchemaError("FRED_TRANSFORM_VALUE_OUT_OF_RANGE") from exc
            last_valid_day = min(current.end, base.end)
            # FRED clips realtime_end to the requested as-of date. That boundary
            # is not evidence of invalidation; only an earlier end closes a value.
            valid_until = (datetime.combine(last_valid_day + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
                           if last_valid_day < cutoff else None)
            observations.append(ProviderObservation(
                provider="fred", provider_series_id=source.series or "", observed_at=period,
                released_at=datetime.combine(event, datetime.min.time(), tzinfo=UTC),
                revision=event.isoformat(), value=value, source_url=str(source.source_url),
                valid_until=valid_until))
    return tuple(observations)
