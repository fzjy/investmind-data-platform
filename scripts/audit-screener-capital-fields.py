"""从五年估值、股东及分红资料复核选股器资本相关阈值。"""

import argparse
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from math import isclose
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import select, text
from app.config import settings
from app.db.models import DailyBasic, FinancialIndicator, PublishedSnapshot, Repurchase, TopHolder, TradeCalendar
from app.shared.readonly_database import open_readonly_factory
from app.shared.stock_mapping import StockMappingPolicy
from app.stocks.derived.inputs import load_metric_card_inputs
from app.stocks.derived.risk_inputs import load_risk_inputs
from app.stocks.shareholder_returns import paid_basis_conflict
from app.stocks.valuation_calendar import load_calendar
from app.stocks.valuation_supply import load_valuation_rows


def decimal(v):
    try:
        n = Decimal(str(v))
        return n if n.is_finite() else None
    except InvalidOperation:
        return None


def latest_roe(rows):
    if not rows:
        return None, {}
    period = max(r.end_date for r in rows)
    candidates = [r for r in rows if r.end_date == period]
    order = ("1", "4", "5", "11", "2", "3", "6", "9", "10", "12", "7", "8")
    report_type = min((r.report_type or "" for r in candidates),
                      key=lambda name: (order.index(name) if name in order else len(order), name))
    candidates = [r for r in candidates if (r.report_type or "") == report_type]
    rank = lambda r: (r.f_ann_date or r.ann_date, int(r.update_flag) if (r.update_flag or "").isdigit() else -1,
                      r.ann_date or date.min)
    best_rank = max(map(rank, candidates))
    selected = [r for r in candidates if rank(r) == best_rank]
    values = {decimal((r.data or {}).get("roe")) for r in selected}
    if len(values) > 1:
        raise ValueError(f"{selected[0].ts_code} 最新同版 ROE 有冲突")
    return values.pop(), {"period": str(period), "visible": str(best_rank[0]), "reportType": report_type,
                          "coreIds": sorted(r.id for r in selected)}


def valuations(history, basics, start, end, expected):
    cutoff = max((r.sourceAsOf for r in history), default=date.min)
    days = {r.trade_date: r for r in history if start <= r.trade_date <= end}
    days.update({r.trade_date: r for r in basics if max(start, cutoff) <= r.trade_date <= end
                 and r.trade_date > cutoff})
    values = {}
    profile = {"rawThrough": str(cutoff) if history else None, "marketDays": len(expected),
               "missingDates": sorted(str(d) for d in expected - set(days)),
               "extraDates": sorted(str(d) for d in set(days) - expected)}
    for key in ("pe", "pb"):
        curve = {day: decimal(getattr(r, key)) for day, r in days.items()}
        current = curve.get(end)
        positive = [v for v in curve.values() if v is not None and v > 0]
        ranked = sum(v <= current for v in positive) if current is not None else 0
        valid = set(days) == expected and end in expected and None not in curve.values() and current and current > 0
        values[key + "Percentile"] = Decimal(100) * ranked / len(positive) if valid and positive else None
        ordered = [f"{d}:{curve[d]}" for d in sorted(curve)]
        profile[key] = dict(current=current, positive=len(positive), atOrBelow=ranked,
                            missingObservations=sum(v is None for v in curve.values()),
                            seriesHash=hashlib.sha256("\n".join(ordered).encode()).hexdigest())
    return values, profile


def holding_period(rows, basics, policy):
    period = rows[0].end_date
    day = max(r.ann_date for r in rows)
    current = [r for r in rows if r.ann_date == day]
    usable = [r for r in basics if 0 <= (period-r.trade_date).days <= policy.institution.maxDenominatorLagDays]
    capital = max(usable, key=lambda r: r.trade_date, default=None)
    shares = decimal((capital.data or {}).get("total_share")) if capital else None
    owner = defaultdict(set)
    for row in current:
        explicit = (row.data or {}).get("hold_ratio")
        pct = decimal(explicit)
        if explicit in (None, "") and shares and shares > 0 and row.hold_amount is not None:
            pct = row.hold_amount / (shares * 10000) * 100
        owner[(row.holder_name or "").strip()].add((row.holder_type, pct))
    info = {"period": str(period), "announced": str(day), "coreRowIds": sorted(r.id for r in current),
            "eligibleDailyBasicId": capital.id if capital else None}
    if "" in owner or any(len(v) != 1 for v in owner.values()):
        return None, info | {"reason": "持有人名称缺失或版本冲突"}
    observations = [next(iter(v)) for v in owner.values()]
    if any(pct is None or not 0 <= pct <= 100 for _, pct in observations):
        return None, info | {"reason": "缺持股比例或有效分母"}
    if sum(pct for _, pct in observations) > Decimal("100.000001"):
        return None, info | {"reason": "持股比例合计异常"}
    included = [pct for kind, pct in observations if policy.holder_kind(kind) == "included"]
    if not included and any(policy.holder_kind(kind) == "unknown" for kind, _ in observations):
        return None, info | {"reason": "机构种类无法判断"}
    return sum(included, Decimal(0)), info


def available(entry, end):
    return (entry.get("basis") == "paid-cash-records-v1" and entry.get("covered")
            and entry.get("recordedThrough") == str(end) and entry.get("querySource") and not entry.get("issues"))


def dividends(inputs, end):
    history = inputs["dividends"]
    fiscal = end.year - 1
    streak = 0
    stop = None
    for year in range(fiscal, 0, -1):
        entry = history.get(str(year), {})
        if not available(entry, end):
            stop, streak = [year, "缺完整覆盖"], None
            break
        paid = [e for e in entry["events"] if e.get("status") == "paid" and (decimal(e["cashPerShare"]) or 0) > 0]
        if not paid:
            stop = [year, "完整覆盖、无正数派息"]
            break
        streak += 1
        if entry.get("listedAt", "") and int(entry["listedAt"][:4]) == year:
            stop = [year, "上市财年"]
            break
    annual_profit = {r["period"][:4]: r for r in inputs["financials"] if r["table"] == "income"
                     and r["kind"] == "cumulative" and r["period"].endswith("-12-31")}
    evidence = {}
    ratios = []
    for year in range(fiscal - 2, fiscal + 1):
        entry = history.get(str(year), {})
        cash = Decimal(0) if available(entry, end) else None
        events = []
        for event in entry.get("events", []):
            if event["status"] not in ("paid", "zero"):
                continue
            shares = decimal(event.get("baseShare"))
            dps = decimal(event.get("cashPerShare"))
            boundary = event.get("recordDate") or event.get("exDate")
            confirmed = shares is not None and shares > 0 and event.get("baseDate") and boundary and event["baseDate"] <= boundary and event.get("sourceIds")
            amount = dps * shares * 10000 if confirmed and dps is not None else Decimal(0) if dps == 0 else None
            if paid_basis_conflict(event, entry.get("observations", [])):
                amount = None
            cash = cash + amount if cash is not None and amount is not None else None
            events.append({"dps": dps, "baseSharesWan": shares, "cashYuan": amount, "sourceIds": event.get("sourceIds")})
        income = annual_profit.get(str(year), {})
        profit = decimal(income.get("values", {}).get("n_income_attr_p"))
        if "n_income_attr_p" in income.get("conflicts", []):
            profit = None
        ratio = 100 * cash / profit if cash is not None and profit and profit > 0 else None
        ratios.append(ratio)
        evidence[year] = dict(covered=bool(available(entry, end)), querySource=entry.get("querySource"),
                              profitYuan=profit, incomeIds=income.get("sourceIds", []), events=events,
                              cashYuan=cash, payoutPercent=ratio)
    payout = sum(ratios) / 3 if None not in ratios else None
    return {"dividendYears": streak, "payoutRatio": payout}, {"streakStop": stop, "years": evidence}


def inspect(factory, session):
    snapshot = session.get(PublishedSnapshot, "screener")
    stocks = {r["symbol"]: r for r in snapshot.payload["stocks"]}
    codes = sorted(stocks)
    end = snapshot.as_of.date()
    start = end.replace(year=end.year-5, day=min(end.day, 28) if end.month == 2 else end.day)
    core_calendar = dict(session.execute(select(TradeCalendar.cal_date, TradeCalendar.is_open).where(
        TradeCalendar.exchange == "SSE", TradeCalendar.cal_date.between(start, end))).all())
    if len(core_calendar) == (end - start).days + 1:
        expected = {d for d, opened in core_calendar.items() if opened}
        calendar_source = "core.trade_calendar"
    else:
        saved = load_calendar(factory, end)
        if not saved:
            raise ValueError("缺完整估值交易日历")
        expected = set(saved)
        calendar_source = "verified trade_cal original"
    history = load_valuation_rows(factory, codes, end)
    inputs = load_metric_card_inputs(session, codes, end)
    financing = load_risk_inputs(session, codes, end, dividend_years={c: inputs[c]["dividends"] for c in codes})
    basics, holders, buybacks = defaultdict(list), defaultdict(lambda: defaultdict(list)), defaultdict(list)
    indicators = defaultdict(list)
    for row in session.scalars(select(FinancialIndicator).where(FinancialIndicator.ts_code.in_(codes),
                                                                 FinancialIndicator.end_date <= end)):
        visible = row.f_ann_date or row.ann_date
        if visible is not None and visible <= end and (row.ann_date or date.min) <= end:
            indicators[row.ts_code].append(row)
    for row in session.scalars(select(DailyBasic).where(DailyBasic.ts_code.in_(codes), DailyBasic.trade_date <= end)):
        basics[row.ts_code].append(row)
    for row in session.scalars(select(TopHolder).where(TopHolder.ts_code.in_(codes), TopHolder.ann_date <= end,
                                                         TopHolder.end_date <= end)):
        holders[row.ts_code][row.end_date].append(row)
    for row in session.scalars(select(Repurchase).where(Repurchase.ts_code.in_(codes), Repurchase.ann_date <= end)):
        buybacks[row.ts_code].append(row.id)
    policy = StockMappingPolicy.load()
    evidence, differences, counts = {}, [], defaultdict(Counter)
    for code, stock in sorted(stocks.items()):
        values, daily = valuations(history.get(code, []), basics[code], start, end, expected)
        values["roe"], roe_source = latest_roe(indicators[code])
        periods = []
        for period in sorted(holders[code])[-2:]:
            pct, details = holding_period(holders[code][period], basics[code], policy)
            periods.append(details | {"percent": pct})
        p = [r["percent"] for r in periods]
        values["institutionHolding"] = p[-1] if p else None
        values["institutionHoldingChange"] = p[-1] - p[0] if len(p) == 2 and None not in p else None
        div, cash = dividends(inputs[code], end)
        values.update(div)
        structural = {}
        gross_by_year = {}
        for year in range(end.year - 10, end.year):
            statement = financing[code]["reports"].get("cashflow", {}).get(f"{year}-12-31", {})
            gross_by_year[year] = decimal(statement.get("values", {}).get("c_recp_cap_contrib"))
        for key in ("capitalReturnRatio", "capitalReturnRatioCum3y", "capitalReturnRatioCum5y", "capitalReturnRatioCum10y",
                    "longMoneyHoldingRatio", "fundHeavyCount", "chipConcentrationChange"):
            values[key] = None
            structural[key] = {"researchKeyPresent": key in stock["researchValues"], "topLevel": stock.get(key),
                               "publishedReason": stock["listMissingReasons"].get(key)}
        for width in (1, 3, 5, 10):
            key = "capitalReturnRatio" if width == 1 else f"capitalReturnRatioCum{width}y"
            amounts = [gross_by_year[y] for y in range(end.year-width, end.year)]
            zero = all(v == 0 for v in amounts)
            structural[key]["grossFinancingZeroEveryYear"] = zero
            reason = structural[key]["publishedReason"] or ""
            matches = ("无融资" in reason) if zero else ("缺完成回购金额" in reason)
            if not matches:
                differences.append({"symbol": code, "key": key, "publishedReason": reason,
                                    "grossFinancingZeroEveryYear": zero})
        cells = {}
        for key, computed in values.items():
            actual = stock["researchValues"].get(key, stock.get(key))
            equal = (actual is None and computed is None) or (actual is not None and computed is not None
                    and isclose(float(computed), actual, rel_tol=1e-9, abs_tol=1e-8))
            counts[key]["different" if not equal else "bothNull" if actual is None else "equalValue"] += 1
            cells[key] = {"recomputed": computed, "published": actual}
            if not equal:
                differences.append({"symbol": code, "key": key, **cells[key]})
        evidence[code] = {"valuation": daily, "roeSource": roe_source, "holdings": periods, "dividends": cash, "structural": structural,
                          "grossFinancingYuanByYear": gross_by_year, "visibleRepurchaseIds": sorted(buybacks[code]),
                          "holderHistoryPoints": len(stock["listHistory"].get("holders", [])), "cells": cells}
    return {"asOf": str(end), "version": snapshot.version, "marketCalendar": calendar_source,
            "symbols": len(stocks), "fieldCounts": counts, "differences": differences, "stockEvidence": evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        if session.scalar(text("show transaction_read_only")) != "on":
            raise RuntimeError("需要只读事务")
        result = inspect(factory, session)
    args.output.write_text(json.dumps(result, ensure_ascii=False, default=str, separators=(",", ":")) + "\n",
                           encoding="utf-8")
    if result["differences"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
