from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def _slot(day, schedule):
    return datetime.combine(day, schedule.time, ZoneInfo(schedule.timezone)).astimezone(UTC)


def most_recent_market_schedule(now: datetime, schedule) -> datetime:
    if now.utcoffset() is None:
        raise ValueError('MARKET_AWARE_CLOCK_REQUIRED')
    day = now.astimezone(ZoneInfo(schedule.timezone)).date()
    slot = _slot(day, schedule)
    return slot if slot <= now.astimezone(UTC) else _slot(day - timedelta(days=1), schedule)


def next_market_schedule(now: datetime, schedule) -> datetime:
    latest = most_recent_market_schedule(now, schedule)
    day = latest.astimezone(ZoneInfo(schedule.timezone)).date() + timedelta(days=1)
    return _slot(day, schedule)
