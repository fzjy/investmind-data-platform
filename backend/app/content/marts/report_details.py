"""把正式财报公告关联到同一证券、报告期与披露日的 Tushare 财务数据。"""
import re
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select

from app.db.models import FinancialIndicator, IncomeStatement, Security
from app.shared.stock_mapping import finite_decimal


REPORT_ENDS = {
    "年度": (12, 31), "半年度": (6, 30), "中期": (6, 30),
    "第一季度": (3, 31), "第三季度": (9, 30),
}
REPORT_TITLE = re.compile(
    r"((?:19|20)\d{2})年?(年度|半年度|中期|第一季度|第三季度)报告(?:全文|摘要)?"
    r"(?:[（(](?:修订版|更新版|更正版|修订稿|更新后|更正后)[）)])?"
)


def report_period(title, issuer=""):
    name = re.split(r"[:：]", "".join(title.split()))[-1]
    if issuer:
        name = name.removeprefix(issuer)
    match = REPORT_TITLE.fullmatch(name)
    if match:
        return date(int(match[1]), *REPORT_ENDS[match[2]])
    return None


def previous_quarter(period):
    return period.replace(month=period.month - 2, day=1) - timedelta(days=1)


def forecast_period(title):
    match = re.search(r"((?:19|20)\d{2})年?(半年度|年度|中期|第一季度|第三季度|前三季度)业绩预告(?:的自愿性披露公告|修正公告|更正公告|公告)?$", "".join(title.split()))
    if match:
        quarter = "第三季度" if match[2] == "前三季度" else match[2]
        return date(int(match[1]), *REPORT_ENDS[quarter])


def version_rows(index, period, cutoff, exact):
    eligible = [(day, row) for day, row in index.get(period, ()) if day <= cutoff and (not exact or day == cutoff)]
    latest = max((day for day, _ in eligible), default=None)
    return [row for day, row in eligible if day == latest]


def field(rows, key):
    values = {finite_decimal(row.data.get(key)) for row in rows}
    return values.pop() if len(values) == 1 else None


def quarter_values(income, indicators, period, cutoff, *, exact):
    rows = version_rows(income, period, cutoff, exact)
    single = [r for r in rows if r.report_type == "2"]
    cumulative = [r for r in rows if r.report_type == "1"]
    prior = [r for r in version_rows(income, previous_quarter(period), cutoff, False) if r.report_type == "1"]
    values = {}
    conflicts = set()
    for key in ("revenue", "n_income_attr_p"):
        value = field(single, key)
        if len({finite_decimal(row.data.get(key)) for row in single} - {None}) > 1:
            conflicts.add(key)
        elif value is None:
            value = field(cumulative, key)
            if period.month != 3:
                comparable = {r.comp_type for r in cumulative} == {r.comp_type for r in prior}
                base = field(prior, key) if comparable else None
                value = value - base if value is not None and base is not None else None
        values[key] = value
    indicator = version_rows(indicators, period, cutoff, exact)
    values.update(q_eps=field(indicator, "q_eps"), q_roe=field(indicator, "q_roe"))
    conflicts.update(key for key in ("q_eps", "q_roe")
                     if len({finite_decimal(row.data.get(key)) for row in indicator} - {None}) > 1)
    return values, conflicts


def attach_report_details(session, workbenches):
    if not any(content.announcements for content in workbenches.values()):
        return
    names = dict(session.execute(select(Security.ts_code, Security.name)
                                 .where(Security.ts_code.in_(workbenches))).all())
    targets = [(symbol, item, period) for symbol, content in workbenches.items()
               for item in content.announcements if (period := report_period(item["title"], names.get(symbol, "")))]
    if not targets:
        return
    symbols = {symbol for symbol, _, _ in targets}
    periods = {period for _, _, period in targets}
    prior = {previous_quarter(period) for period in periods}
    needed = periods | prior | {previous_quarter(period) for period in prior}
    indexes = []
    for model in (IncomeStatement, FinancialIndicator):
        index = defaultdict(lambda: defaultdict(list))
        query = select(model).where(model.ts_code.in_(symbols), model.end_date.in_(needed))
        if model is IncomeStatement:
            query = query.where(model.report_type.in_(("1", "2")))
        for row in session.scalars(query):
            day = max((d for d in (row.ann_date, row.f_ann_date) if d), default=None)
            if day and day >= row.end_date:
                index[row.ts_code][row.end_date].append((day, row))
        indexes.append(index)

    income, indicators = indexes
    for symbol, item, period in targets:
        cutoff = date.fromisoformat(item["disclosedOn"])
        previous = previous_quarter(period)
        current, conflicts = quarter_values(income[symbol], indicators[symbol], period, cutoff, exact=True)
        before, prior_conflicts = quarter_values(income[symbol], indicators[symbol], previous, cutoff, exact=False)
        metrics = {}
        for key, value in current.items():
            base = before[key]
            delta = value - base if value is not None and base is not None else None
            reason = None
            if value is None:
                reason = "同日版本数值不一致" if key in conflicts else "本期单季数据待核对"
            elif base is None:
                reason = "上季版本数值不一致" if key in prior_conflicts else "缺可比上季数据"
            elif base == 0:
                reason = "上季为零"
            metrics[key] = {
                "value": float(value) if value is not None else None,
                "changePercent": float(delta / abs(base) * 100) if delta is not None and base else None,
                "reason": reason,
            }
            if key == "q_roe" and delta is not None:
                metrics[key]["changePoints"] = float(delta)
        item.update(kind="earnings", reportPeriod=period.isoformat(),
                    financialReport={"comparisonPeriod": previous.isoformat(), "metrics": metrics})
