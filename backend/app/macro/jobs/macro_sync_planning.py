from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.connectors.macro.contracts import MacroFetchWindow
from app.macro.jobs.macro_series_state import MacroSeriesStateView
from app.macro.catalog import MacroFrequency, MacroSeriesCatalog, MacroSeriesDefinition


@dataclass(frozen=True, slots=True)
class MacroSyncOptions:
    as_of: datetime
    history_years: int
    catalog_version: str
    catalog_hash: str
    force: bool = False
    trigger: Literal["cli", "scheduler"] = "cli"
    mode: Literal["replay", "live"] = "replay"
    series_ids: tuple[str, ...] = ()
    publish: bool = True

    def __post_init__(self) -> None:
        if self.as_of.utcoffset() is None:
            raise ValueError("as_of must have a timezone")
        if self.history_years < 1:
            raise ValueError("history_years must be positive")
        if self.mode not in ("replay", "live"):
            raise ValueError("mode must be replay or live")
        if len(set(self.series_ids)) != len(self.series_ids) or (self.series_ids and self.publish):
            raise ValueError('scoped sync requires unique series and explicit publication')


@dataclass(frozen=True, slots=True)
class MacroWorkItem:
    series: MacroSeriesDefinition
    window: MacroFetchWindow


class MacroSyncPlanner:
    def __init__(self, catalog: MacroSeriesCatalog, *, timezone: str | None = None) -> None:
        self._catalog = catalog
        self._timezone = ZoneInfo(timezone) if timezone is not None else None

    def plan(
        self,
        options: MacroSyncOptions,
        states: Mapping[str, MacroSeriesStateView],
    ) -> tuple[MacroWorkItem, ...]:
        for series_id in options.series_ids:
            self._catalog.require(series_id)
        end = self.end_date(options.as_of)
        boundary = _shift_months(end, -12 * options.history_years)
        return tuple(
            MacroWorkItem(
                series,
                MacroFetchWindow(
                    start=_start(
                        series.frequency, end, boundary,
                        _visible_planning_state(states.get(series.id), options.as_of, end),
                        self._catalog,
                    ),
                    end=end,
                    as_of=options.as_of,
                ),
            )
            for series in self._catalog.series
            if not options.series_ids or series.id in options.series_ids
        )

    def end_date(self, as_of: datetime) -> date:
        return as_of.astimezone(self._timezone).date() if self._timezone is not None else as_of.date()


def state_is_future(
    state: MacroSeriesStateView | None, as_of: datetime, end: date,
) -> bool:
    if state is None:
        return False
    return any(
        value is not None and value > as_of
        for value in (state.last_checked_at, state.last_success_at, state.latest_released_at)
    ) or (state.latest_observed_at is not None and state.latest_observed_at > end)


def _visible_planning_state(
    state: MacroSeriesStateView | None, as_of: datetime, end: date,
) -> MacroSeriesStateView | None:
    return None if state_is_future(state, as_of, end) else state


def _start(
    frequency: MacroFrequency,
    end: date,
    boundary: date,
    state: MacroSeriesStateView | None,
    catalog: MacroSeriesCatalog,
) -> date:
    if state is None or state.latest_observed_at is None:
        return boundary
    overlap = catalog.defaults.overlap
    if frequency == "daily":
        start = end - timedelta(days=overlap.daily_days - 1)
        observed = state.latest_observed_at
    elif frequency == "monthly":
        start = _shift_months(end.replace(day=1), -overlap.monthly_months)
        observed = state.latest_observed_at.replace(day=1)
    else:
        quarter_start = end.replace(month=((end.month - 1) // 3) * 3 + 1, day=1)
        start = _shift_months(quarter_start, -3 * (overlap.quarterly_quarters - 1))
        observed = state.latest_observed_at.replace(
            month=((state.latest_observed_at.month - 1) // 3) * 3 + 1,
            day=1,
        )
    return max(boundary, min(start, observed))


def _shift_months(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    if month == 12:
        following = date(year + 1, 1, 1)
    else:
        following = date(year, month + 1, 1)
    last_day = (following - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))
