"""按工作台截止日批量取得风险计算使用的报表修订、估值及分红。"""
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func

from app.db.models import BalanceSheet, CashflowStatement, DailyBasic, DailyPrice, FinancialIndicator, IncomeStatement
from app.shared.stock_mapping import finite_decimal
from app.stocks.dataset_query import daily_rows_query, financial_rows_query
from app.stocks.derived.inputs import metric_report_revisions
from app.stocks.derived.metric_dividends import load_dividend_years
from app.stocks.derived.workbench_cards import DEBT_FIELDS
from app.stocks.financial_supply import metric_card_originals


FIELDS = {
    "income": ("n_income_attr_p", "t_compr_income", "invest_income", "fv_value_chg_gain",
               "non_oper_income", "non_oper_exp", "assets_impair_loss", "credit_impa_loss",
               "revenue", "basic_eps", "ebit", "fin_exp_int_exp"),
    "balance": ("total_assets", "money_cap", "goodwill", "total_hldr_eqy_inc_min_int",
                "total_share", *DEBT_FIELDS),
    "cashflow": ("n_cashflow_act", "c_recp_borrow", "c_recp_cap_contrib", "proc_issue_bonds"),
    "indicator": ("interestdebt",),
}


def load_risk_inputs(session, symbols, as_of, *, dividend_years=None):
    codes = tuple(sorted(set(symbols)))
    dividends = dividend_years if dividend_years is not None else load_dividend_years(session, codes, as_of)
    try:
        start = as_of.replace(year=as_of.year - 10)
    except ValueError:
        start = (as_of - timedelta(days=1)).replace(year=as_of.year - 10)
    result = {code: dict(asOf=as_of.isoformat(), start=start.isoformat(), reports={},
                         valuations=[], dividends=dividends[code]) for code in codes}
    originals = defaultdict(list)
    for kind, row in metric_card_originals(session, codes, as_of, report_fields=FIELDS):
        originals[kind].append(row)
    for kind, model in (("income", IncomeStatement), ("balance", BalanceSheet),
                        ("cashflow", CashflowStatement), ("indicator", FinancialIndicator)):
        query = financial_rows_query(model, codes, end=as_of).where(
                                    func.coalesce(model.f_ann_date, model.ann_date) <= as_of)
        groups = metric_report_revisions([*session.scalars(query), *originals[kind]], indicator=kind == "indicator", as_of=as_of)
        for (code, period, mode), rows in groups.items():
            if mode != "cumulative":
                continue
            values = {}
            for field in FIELDS[kind]:
                observed = {finite_decimal(row.data[field]) for row in rows if field in row.data}
                if len(observed) == 1:
                    value = observed.pop()
                    if value is not None:
                        values[field] = str(value)
            first = rows[0]
            result[code]["reports"].setdefault(kind, {})[period.isoformat()] = dict(
                date=(first.f_ann_date or first.ann_date).isoformat(), values=values)
    from app.stocks.valuation_supply import risk_valuation_history
    history = risk_valuation_history(session, codes, as_of)
    query = daily_rows_query(DailyBasic, start, as_of, codes).with_only_columns(
        DailyBasic.ts_code, DailyBasic.trade_date, DailyBasic.pe, DailyBasic.pb).order_by(DailyBasic.trade_date)
    core = defaultdict(dict)
    for code, day, pe, pb in session.execute(query):
        core[code][day.isoformat()] = (pe, pb)
    trading_days = defaultdict(set)
    for code, day in session.execute(daily_rows_query(DailyPrice, start, as_of, codes)
                                     .with_only_columns(DailyPrice.ts_code, DailyPrice.trade_date)):
        trading_days[code].add(day.isoformat())
    for code, data in result.items():
        entry = history.get(code, {})
        rows = {day: pair for day, pair in entry.get("rows", {}).items() if day >= data["start"]}
        conflicts = []
        for day, pair in core[code].items():
            if day in rows and tuple(map(finite_decimal, rows[day])) != pair:
                conflicts.append(day)
                rows[day] = [v if finite_decimal(raw) == v else None for raw, v in zip(rows[day], pair)]
            else:
                rows[day] = pair
        data["valuations"] = [[day, *(str(v) if v is not None else None for v in pair)]
                              for day, pair in sorted(rows.items())]
        gaps = sorted(trading_days[code].difference(rows))
        issues = []
        for days, label in ((gaps, "有交易行情但缺少估值"), (sorted(conflicts), "批量原件与日更估值不一致")):
            if days:
                issues.append(f"{label}，共 {len(days)} 个交易日，首日 {days[0]}，末日 {days[-1]}")
        if issues:
            data["valuationIssues"] = issues
        data["windowVerified"] = bool(entry and entry["start"] <= data["start"])
    return result
