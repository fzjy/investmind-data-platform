"""从合同验真的原始财报和核心表重算选股器八组财年指标。"""

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from math import isclose
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import extract, select, text
from app.config import settings
from app.db.models import BalanceSheet, CashflowStatement, IncomeStatement, PublishedSnapshot
from app.shared.readonly_database import open_readonly_factory
from app.stocks.financial_supply import load_points


TABLES = {"income_statement": IncomeStatement, "balance_sheet": BalanceSheet, "cashflow_statement": CashflowStatement}
FIELDS = {
    "income_statement": ("n_income_attr_p", "revenue", "total_revenue", "oper_cost"),
    "balance_sheet": ("total_liab",),
    "cashflow_statement": ("n_cashflow_act", "c_pay_acq_const_fiolta", "c_fr_sale_sg", "c_paid_goods_s"),
}
RATIOS = {
    "realMoneyIndex": ("n_cashflow_act", "n_income_attr_p"),
    "ocfToRevenue": ("n_cashflow_act", "revenue"),
    "ocfToTotalLiab": ("n_cashflow_act", "total_liab"),
    "capexCoverageRatio": ("n_cashflow_act", "c_pay_acq_const_fiolta"),
    "cashCollectionRatio": ("c_fr_sale_sg", "revenue"),
    "cashPaymentRatio": ("c_paid_goods_s", "oper_cost"),
}
METRICS = (*RATIOS, "freeCashFlowFy", "freeCashFlowMarginFy")
WIDTHS = (1, 3, 5, 10)


def amount(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def iso_day(value):
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value))
    return date.min


@dataclass
class Report:
    code: str
    table: str
    year: int
    kind: str
    visible: date
    announced: date
    flag: str
    origin: str
    reference: str
    values: dict

    @property
    def rank(self):
        return (self.kind != "5", self.visible, self.kind == "4", self.flag, self.announced)


def source_reports(factory, session, as_of, codes):
    # load_points 仅核验已存合同、请求、原始响应和完成批次；本脚本自行选报告及重算。
    points = load_points(factory, as_of, codes, allow_new_price_day=True)
    for p in points:
        if p.end_date.month != 12 or p.end_date.day != 31 or p.report_type != "1":
            continue
        announced = iso_day(p.values.get("ann_date"))
        if not p.visible_at or not p.end_date <= p.visible_at <= as_of or announced > as_of:
            continue
        yield Report(p.ts_code, p.source_table.split(".")[1], p.end_date.year, p.report_type,
                     p.visible_at, announced, str(p.values.get("update_flag") or ""), "raw",
                     p.values["_sourceId"], p.values)
    for table, model in TABLES.items():
        query = select(model).where(model.ts_code.in_(codes), model.end_date <= as_of,
            extract("month", model.end_date) == 12, extract("day", model.end_date) == 31,
            model.report_type.in_(("1", "4", "5")))
        for row in session.scalars(query):
            visible = row.f_ann_date or row.ann_date
            if not visible or not row.end_date <= visible <= as_of or (row.ann_date and row.ann_date > as_of):
                continue
            yield Report(row.ts_code, table, row.end_date.year, row.report_type, visible,
                         row.ann_date or date.min, str(row.update_flag or ""), "core",
                         f"core.{table}:{row.id}", row.data or {})


def select_filing(reports, table):
    rank = max(r.rank for r in reports)
    selected = [r for r in reports if r.rank == rank]
    # 原件工作区只吸纳现金流核心表作同版扩展；其余两表有同版原件时以原件为准。
    if any(r.origin == "raw" for r in selected) and table != "cashflow_statement":
        selected = [r for r in selected if r.origin == "raw"]
    values = {}
    conflicts = []
    for key in FIELDS[table]:
        observed = {amount(r.values[key]) for r in selected if key in r.values}
        values[key] = next(iter(observed)) if len(observed) == 1 else None
        if len(observed) > 1:
            conflicts.append(key)
    filing = selected[0]
    version = {"type": filing.kind, "visible": str(filing.visible), "announced": str(filing.announced),
               "flag": filing.flag, "ids": sorted({r.reference for r in selected})}
    if conflicts:
        version["conflicts"] = conflicts
    return values, version


def recalculate(series, metric, years):
    missing_years = [f"{y}.缺年报" for y in years if y not in series]
    if missing_years:
        return None, missing_years
    fields = RATIOS.get(metric, ("n_cashflow_act", "c_pay_acq_const_fiolta"))
    if metric == "freeCashFlowMarginFy":
        fields = (*fields, "revenue")
    missing = [f"{year}.{field}" for field in fields for year in years if series[year].get(field) is None]
    if missing:
        return None, missing
    totals = [sum(series[y][k] for y in years) for k in fields]
    if metric in RATIOS:
        return (totals[0] / totals[1], []) if totals[1] else (None, [f"{fields[1]}合计为零"])
    if any(series[y]["c_pay_acq_const_fiolta"] < 0 for y in years):
        return None, ["负资本开支"]
    free_cash = totals[0] - totals[1]
    if metric == "freeCashFlowFy":
        return free_cash / Decimal(100_000_000), []
    return (free_cash / totals[2], []) if totals[2] else (None, ["营收合计为零"])


def inspect(factory, session):
    snapshot = session.get(PublishedSnapshot, "screener")
    as_of = snapshot.as_of.date()
    rows = snapshot.payload["stocks"]
    universe = {row["symbol"]: row for row in rows}
    if not rows or len(rows) != len(universe):
        raise ValueError("已发布选股池为空或代码重复")
    grouped = defaultdict(list)
    origins = Counter()
    for report in source_reports(factory, session, as_of, sorted(universe)):
        grouped[report.code, report.year, report.table].append(report)
        origins[report.origin] += 1
    if not origins["raw"]:
        raise ValueError("未取得合同验真的完整财报原件批次")
    summary = defaultdict(Counter)
    stock_reports = {}
    discrepancies = []
    for code, published in sorted(universe.items()):
        annual = {}
        provenance = {}
        visible_years = sorted({year for symbol, year, _ in grouped if symbol == code})
        if not visible_years:
            raise ValueError(f"{code} 缺可见年报")
        end_year = max(visible_years)
        for year in range(end_year - 9, end_year + 1):
            facts = {}
            source = {}
            for table in TABLES:
                reports = grouped.get((code, year, table))
                if reports:
                    values, source[table] = select_filing(reports, table)
                    facts.update(values)
            if not facts:
                continue
            revenue_key = "revenue" if facts.get("revenue") is not None else "total_revenue"
            facts["revenue"] = facts.get(revenue_key)
            annual[year] = facts
            provenance[year] = {"reports": source, "revenueField": revenue_key,
                                "yuan": {k: str(v) if v is not None else None for k, v in facts.items()}}
        values_by_metric = {}
        for metric in METRICS:
            by_window = {}
            for width in WIDTHS:
                key = metric if width == 1 else f"{metric}Cum{width}y"
                computed, reason = recalculate(annual, metric, range(end_year-width+1, end_year+1))
                research = published["researchValues"]
                if key not in research:
                    raise ValueError(f"{code} 缺发布字段 {key}")
                actual = research[key]
                if computed is None and actual is None:
                    status = "bothNull"
                elif computed is not None and actual is not None and isclose(float(computed), actual,
                        rel_tol=1e-9, abs_tol=1e-8):
                    status = "equalValue"
                else:
                    status = "difference"
                    discrepancies.append({"code": code, "key": key, "published": actual,
                                          "recomputed": str(computed), "reason": reason})
                summary[key][status] += 1
                by_window[width] = {"recomputed": float(computed) if computed is not None else None,
                                    "published": actual}
                if reason:
                    by_window[width]["missing"] = reason
            values_by_metric[metric] = by_window
        stock_reports[code] = {"latestFiscalYear": end_year, "annualInputs": provenance,
                               "cells": values_by_metric}
    return {"asOf": str(as_of), "version": snapshot.version, "symbols": len(universe),
            "sourceAnnualCandidates": origins, "fieldCounts": dict(summary),
            "differences": discrepancies, "stockEvidence": stock_reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        if session.scalar(text("show transaction_read_only")) != "on":
            raise RuntimeError("需要只读事务")
        result = inspect(factory, session)
    evidence = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if args.output:
        args.output.write_text(evidence + "\n", encoding="utf-8")
    else:
        print(evidence)
    if result["differences"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
