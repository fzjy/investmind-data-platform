"""中国货币网十年期国债的同日基准与历史估计日证据。"""
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite

from sqlalchemy import text

from app.macro.api.observations import select_visible_observations, stored_observations

SERIES = 'cn.rate.govbond_10y'
SOURCE = 'chinamoney'


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _point(row):
    rate = float(Decimal(str(row.value)) / 100)
    if row.unit != '%' or not isfinite(rate) or not row.source_url:
        raise ValueError('invalid treasury observation')
    return dict(date=row.observed_at.isoformat(), value=rate,
                sourceId=f'core.macro_observation:{row.id}', releasedAt=_utc(row.released_at).isoformat(),
                revision=row.revision)


def build_treasury_reference(rows, as_of: date, start: str):
    cutoff = datetime.combine(as_of, time.max, timezone.utc)
    rows = [r for r in rows if r.source == SOURCE and r.series_id == SERIES
            and r.observed_at <= as_of and _utc(r.released_at) <= cutoff]
    model = dict(status='missing', asOf=as_of.isoformat(), seriesId=SERIES, unit='annual-rate',
                 points=[], revisions=[])
    visible = select_visible_observations(rows, cutoff, {SERIES: (SOURCE,)})
    try:
        points = [_point(row) for row in visible if row.observed_at.isoformat() >= start]
        revisions = []
        for row in sorted(rows, key=lambda r: (r.observed_at, _utc(r.released_at), r.revision)):
            point = _point(row)
            if row.valid_until is not None and _utc(row.valid_until) <= cutoff:
                point['validUntil'] = _utc(row.valid_until).isoformat()
            revisions.append(point)
    except (InvalidOperation, ValueError, OverflowError):
        return model | dict(status='invalid')
    candidate = model | dict(status='observed' if points else 'missing', points=points, revisions=revisions)
    return candidate if valid_treasury_reference(candidate, model['asOf']) else model | dict(status='invalid')


def load_treasury_reference(factory, as_of, start):
    with factory(autoflush=False) as session:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        # 最近已公开的观测可能早于证券价格窗口。基准 points 仍沿用原窗口。
        rows = stored_observations(session, (SERIES,), datetime.combine(as_of, time.max, timezone.utc),
                                   end=as_of, source=SOURCE)
        return build_treasury_reference(rows, as_of, start)


def _instant(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError('treasury timestamp requires a UTC offset')
    return _utc(moment)


def valid_treasury_reference(model, as_of):
    try:
        if (not isinstance(model, dict) or model.get('status') not in ('observed', 'missing')
                or model.get('asOf') != as_of or model.get('seriesId') != SERIES
                or model.get('unit') != 'annual-rate'):
            return False
        cutoff = datetime.combine(date.fromisoformat(as_of), time.max, timezone.utc)
        current = {}
        for field in ('points', 'revisions'):
            if field == 'revisions' and field not in model:
                continue
            if not isinstance(model[field], list):
                return False
            seen = set()
            for point in model[field]:
                day = date.fromisoformat(point['date'])
                released = _instant(point['releasedAt'])
                identity = day if field == 'points' else (day, released, point['revision'])
                if (day.isoformat() != point['date'] or day > cutoff.date() or released > cutoff
                        or identity in seen or type(point['value']) not in (int, float)
                        or not isfinite(point['value']) or not isinstance(point['sourceId'], str)
                        or not point['sourceId'] or not isinstance(point['revision'], str)
                        or not point['revision']):
                    return False
                expires = _instant(point['validUntil']) if 'validUntil' in point else None
                if expires is not None and expires <= released:
                    return False
                seen.add(identity)
                if field == 'revisions':
                    rank = (released, point['revision'])
                    if day not in current or rank > current[day][0]:
                        current[day] = (rank, None if expires and expires <= cutoff else point)
        if 'revisions' in model:
            for point in model['points']:
                selected = current.get(date.fromisoformat(point['date']))
                if not selected or not selected[1] or any(selected[1][k] != v for k, v in point.items()):
                    return False
        return bool(model['points']) == (model['status'] == 'observed')
    except (KeyError, ValueError, TypeError, OverflowError):
        return False
