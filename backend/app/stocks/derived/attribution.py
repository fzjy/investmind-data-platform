"""按披露日期保留组合盈利归因的历史可见版本。"""
from datetime import date

from sqlalchemy import select

from app.db.models import IncomeStatement
from app.shared.stock_mapping import finite_decimal
from app.stocks.derived.inputs import metric_report_revisions, ttm_value


def _known_at(row):
    return max(day for day in (row.f_ann_date, row.ann_date) if day is not None)


def earnings_timeline(rows, as_of):
    rows = [row for row in rows if (row.f_ann_date or row.ann_date)
            and _known_at(row) <= as_of and row.end_date <= as_of
            and row.report_type in {"1", "4", "5"}]
    result = []
    for known_at in sorted({_known_at(row) for row in rows}):
        revisions = metric_report_revisions([row for row in rows if _known_at(row) <= known_at])
        periods = {period: group for (_, period, kind), group in revisions.items() if kind == "cumulative"}
        latest = max(periods)
        required = [latest]
        if (latest.month, latest.day) != (12, 31):
            required.extend([date(latest.year - 1, 12, 31), latest.replace(year=latest.year - 1)])
        amounts, reasons, sources, visible_dates = [], [], [], []
        for period in required:
            group = periods.get(period, [])
            values = {finite_decimal(row.data["n_income_attr_p"]) for row in group if "n_income_attr_p" in row.data}
            amount = next(iter(values)) if len(values) == 1 else None
            if amount is None:
                reasons.append(f"{period}归母净利润缺失或同版冲突")
            amounts.append(amount)
            sources.extend(getattr(row, "source_id", None) or f"core.income_statement:{row.id}" for row in group)
            visible_dates.extend(_known_at(row) for row in group)
        profit = amounts[0] if len(amounts) == 1 else ttm_value(*amounts)
        entry = dict(knownAt=max(visible_dates).isoformat(), periodEnd=latest.isoformat(),
            reportDisclosedAt=max(_known_at(row) for row in periods[latest]).isoformat(),
            profitTtm=float(profit) if profit is not None and profit.is_finite() else None,
            sourceIds=sorted(set(sources)), reason="；".join(reasons))
        if not result or result[-1] != entry:
            result.append(entry)
    return result


def load_attribution_earnings(session, symbols, as_of):
    from app.stocks.financial_supply import metric_card_originals

    rows = list(session.scalars(select(IncomeStatement).where(
        IncomeStatement.ts_code.in_(symbols), IncomeStatement.end_date <= as_of)))
    rows.extend(row for _, row in metric_card_originals(
        session, symbols, as_of, report_fields={"income": ("n_income_attr_p",)}))
    return {symbol: earnings_timeline([row for row in rows if row.ts_code == symbol], as_of) for symbol in symbols}
