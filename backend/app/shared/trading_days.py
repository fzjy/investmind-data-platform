"""用已发布 K 线、交易日历和个股停牌记录定位 A 股披露事件。"""
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models import Suspension, TradeCalendar


@dataclass
class TradingDays:
    candles: set[date]
    first: date
    last: date
    calendar: dict[date, bool]
    suspended: set[date]

    def locate(self, disclosed: date, clock: time | None = None):
        if disclosed < self.first:
            return None, "outside", "超出图表历史范围"
        day = disclosed
        reasons = []
        if clock is not None and clock >= time(15) and self.calendar.get(day) is not False:
            reasons.append("收盘后披露")
            day += timedelta(days=1)
        while day <= self.last:
            if self.calendar.get(day) is False:
                if day in self.candles:
                    return None, "pending", "日历与个股行情不一致"
                reason = "休市"
            elif day in self.candles:
                if day in self.suspended and day == disclosed and clock is not None:
                    return None, "pending", "当日停复牌时刻待核对"
                return day, "placed", "、".join(reasons)
            elif day in self.suspended:
                reason = "个股停牌"
            else:
                return None, "pending", "交易日历或个股行情待核对"
            if reason not in reasons:
                reasons.append(reason)
            day += timedelta(days=1)
        return None, "pending", "等待后续交易日行情"


def candle_dates(candles):
    return [date.fromisoformat(row["date"]) for row in candles or ()
            if all(type(row.get(key)) in (float, int) and isfinite(row[key])
                   for key in ("open", "high", "low", "close"))]


def chart_history_bounds(days):
    if not days:
        return None
    last = max(days)
    year = last.year - 5
    earliest = date(year, last.month, min(last.day, monthrange(year, last.month)[1]))
    return max(min(days), earliest), last


def load_trading_days(session, prices):
    dates = {symbol: set(days) for symbol, days in prices.items() if days}
    if not dates:
        return {}
    bounds = {symbol: chart_history_bounds(days) for symbol, days in dates.items()}
    first = min(start for start, _ in bounds.values())
    last = max(end for _, end in bounds.values())
    calendars = defaultdict(dict)
    for exchange, day, opened in session.execute(select(TradeCalendar.exchange, TradeCalendar.cal_date,
            TradeCalendar.is_open).where(TradeCalendar.cal_date.between(first, last))):
        calendars[exchange][day] = opened
    suspended = defaultdict(set)
    for symbol, day in session.execute(select(Suspension.ts_code, Suspension.trade_date).where(
            Suspension.ts_code.in_(dates), Suspension.suspend_type == "S", Suspension.trade_date.between(first, last))):
        suspended[symbol].add(day)
    result = {}
    for symbol, days in dates.items():
        exchange = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(symbol.rsplit(".", 1)[-1])
        calendar = calendars.get(exchange) or (calendars["SSE"] if exchange == "SZSE" else {})
        result[symbol] = TradingDays(days, *bounds[symbol], calendar, suspended[symbol])
    return result


def announcement_dates(stamp: datetime, trading: TradingDays | None):
    utc = (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    local = utc.astimezone(ZoneInfo("Asia/Shanghai"))
    # 旧导入合同只存时间戳，UTC 或上海午夜可能是纯日期的占位值。
    clock = None if time.min in (utc.time(), local.time()) else local.time()
    placed, status, reason = (trading.locate(local.date(), clock) if trading
                              else (None, "pending", "等待个股行情"))
    return dict(date=(placed or local.date()).isoformat(), disclosedOn=local.date().isoformat(),
                disclosureTime=clock.isoformat(timespec="minutes") if clock else None,
                placementStatus=status, placementReason=reason)
