from calendar import monthrange
from datetime import date

from app.macro.catalog import MacroSeriesDefinition


def observation_expired(series: MacroSeriesDefinition, observed_at: date, as_of: date) -> bool:
    """月/季度日期标识整个统计期；从期末计算年龄，不修改观测身份或阈值。"""
    if observed_at > as_of:
        return True
    anchor = observed_at
    if series.frequency == "monthly":
        anchor = date(observed_at.year, observed_at.month, monthrange(observed_at.year, observed_at.month)[1])
    elif series.frequency == "quarterly":
        month = ((observed_at.month - 1) // 3 + 1) * 3
        anchor = date(observed_at.year, month, monthrange(observed_at.year, month)[1])
    return (as_of - anchor).days > series.freshness_days
