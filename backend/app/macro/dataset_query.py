"""按观测期、来源优先级、发布时刻与修订有效期读取宏观事实。"""
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MacroObservation
from app.macro.catalog import MacroSeriesCatalog
from app.macro.import_lineage import is_verified_import, verify_import_lineage


def _released_at(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def select_visible_observations(
    rows: Sequence[MacroObservation], as_of: datetime,
    priorities: Mapping[str, Sequence[str]],
) -> tuple[MacroObservation, ...]:
    latest: dict[tuple[str, date, str], MacroObservation] = {}
    for row in rows:
        released_at = _released_at(row.released_at)
        if released_at > as_of or row.observed_at > as_of.date():
            continue
        if row.source not in priorities.get(row.series_id, ()) and not is_verified_import(row):
            continue
        key = (row.series_id, row.observed_at, row.source)
        prior = latest.get(key)
        if prior is None or (_released_at(row.released_at), row.revision) > (_released_at(prior.released_at), prior.revision):
            latest[key] = row
    by_period: dict[tuple[str, date], MacroObservation] = {}
    for row in latest.values():
        # Select the newest revision first. Filtering before selection would
        # resurrect an older open revision when the newest one has expired.
        if row.valid_until is not None and _released_at(row.valid_until) <= as_of:
            continue
        key = (row.series_id, row.observed_at)
        priority = priorities.get(row.series_id, ())
        current = by_period.get(key)
        rank = (priority.index(row.source) if row.source in priority else len(priority), row.source)
        if current is None:
            by_period[key] = row
        else:
            current_rank = (priority.index(current.source) if current.source in priority else len(priority), current.source)
            if rank < current_rank:
                by_period[key] = row
    selected = tuple(by_period[key] for key in sorted(by_period))
    # Preserve duplicate *selected* identities so the shared quality gate can reject
    # them without letting unrelated historical rows block a valid candidate.
    by_identity: dict[tuple[object, ...], list[MacroObservation]] = {}
    for row in rows:
        identity = (row.source, row.series_id, row.observed_at, _released_at(row.released_at), row.revision)
        by_identity.setdefault(identity, []).append(row)
    return tuple(row for chosen in selected for row in by_identity[
        (chosen.source, chosen.series_id, chosen.observed_at, _released_at(chosen.released_at), chosen.revision)])


def select_catalog_observations(rows: Sequence[MacroObservation], catalog: MacroSeriesCatalog, as_of: datetime) -> tuple[MacroObservation, ...]:
    priorities = {item.id: tuple(source.provider for source in sorted(item.sources, key=lambda source: source.priority)) for item in catalog.series}
    return select_visible_observations(tuple(row for row in rows if row.series_id in priorities), as_of, priorities)


def stored_observations(
    session: Session, series_ids: Sequence[str] | None, as_of: datetime, *,
    start: date | None = None, end: date | None = None, after: date | None = None,
    source: str | None = None, period_limit: int | None = None,
) -> tuple[MacroObservation, ...]:
    statement = select(MacroObservation).where(MacroObservation.released_at <= as_of)
    if series_ids is not None:
        statement = statement.where(MacroObservation.series_id.in_(series_ids))
    if source is not None:
        statement = statement.where(MacroObservation.source == source)
    if start is not None:
        statement = statement.where(MacroObservation.observed_at >= start)
    if end is not None:
        statement = statement.where(MacroObservation.observed_at <= end)
    if after is not None:
        statement = statement.where(MacroObservation.observed_at > after)
    if period_limit is not None:
        periods = statement.with_only_columns(MacroObservation.observed_at).distinct().order_by(
            MacroObservation.observed_at).limit(period_limit)
        statement = statement.where(MacroObservation.observed_at.in_(periods))
    rows = tuple(session.scalars(statement))
    verify_import_lineage(session, rows)
    return rows


def observation_page(session, catalog, series_id, cutoff, start, end, after, row_limit):
    # 逐批读完整观测期的所有候选版，避免在选版前 SQL LIMIT 截断同一期。
    result = []
    batch_size = max(64, row_limit)
    while len(result) < row_limit:
        rows = stored_observations(session, (series_id,), cutoff, start=start, end=min(end, cutoff.date()),
                                   after=after, period_limit=batch_size)
        if not rows:
            break
        result.extend(select_catalog_observations(rows, catalog, cutoff))
        periods = {r.observed_at for r in rows}
        if len(periods) < batch_size:
            break
        after = max(periods)
    return result[:row_limit]
