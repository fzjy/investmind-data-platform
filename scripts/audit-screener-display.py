"""从当前选股池可见财报核对年、季趋势，另检验十年估值窗及五项风险显示。"""
from argparse import ArgumentParser
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
import json
from math import isclose
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import select, text
from app.config import settings
from app.db.models import BalanceSheet, CashflowStatement, IncomeStatement, PublishedSnapshot, Security, TradeCalendar, Suspension, DailyPrice
from app.shared.readonly_database import open_readonly_factory
from app.stocks.financial_supply import load_points
from app.stocks.derived.inputs import load_metric_card_inputs
from app.stocks.derived.risk_inputs import load_risk_inputs
from app.stocks.shareholder_returns import load_shareholder_returns
from app.stocks.valuation_calendar import _contracts, _saved

TABLES = {"income_statement": IncomeStatement, "balance_sheet": BalanceSheet, "cashflow_statement": CashflowStatement}
INPUTS = {
    "income_statement": {"revenue": ("revenue", "total_revenue"), "profit": ("n_income_attr_p",),
                         "cost": ("oper_cost",), "netIncome": ("n_income",)},
    "balance_sheet": {"contractLiability": ("contract_liab",), "receivable": ("accounts_receiv",),
                      "inventory": ("inventories",), "payable": ("acct_payable",)},
    "cashflow_statement": {"cashflow": ("n_cashflow_act",), "investmentCash": ("n_cashflow_inv_act",),
                           "financingCash": ("n_cash_flows_fnc_act",)},
}
DEBT = ("st_borr", "lt_borr", "st_bonds_payable", "bond_payable", "lease_liab", "non_cur_liab_due_1y")
QUARTERS = ("03-31", "06-30", "09-30", "12-31")


def number(v):
    return Decimal(str(v)) if v is not None else None


def collect(factory, session, codes, end):
    reports = defaultdict(list)
    def add(code, table, period, kind, visible, announced, flag, reference, values):
        if kind not in ("1", "4", "5") or not visible or not period <= visible <= end:
            return
        if period.isoformat()[5:] not in QUARTERS or (announced and announced > end):
            return
        rank = (kind != "5", visible, kind == "4", str(flag or ""), announced or date.min)
        reports[code, period, table].append((rank, reference, values))
    raw = load_points(factory, end, codes, allow_new_price_day=True)
    if not raw:
        raise ValueError("未找到合同核验通过的报表原件")
    for p in raw:
        table = p.source_table.split(".")[-1]
        if table not in TABLES or p.report_type != "1":
            continue
        ann = p.values.get("ann_date")
        add(p.ts_code, table, p.end_date, p.report_type, p.visible_at,
            date.fromisoformat(str(ann)) if ann else None, p.values.get("update_flag"), p.values["_sourceId"], p.values)
    for table, model in TABLES.items():
        for r in session.scalars(select(model).where(model.ts_code.in_(codes), model.end_date <= end)):
            add(r.ts_code, table, r.end_date, r.report_type, r.f_ann_date or r.ann_date,
                r.ann_date, r.update_flag, f"core.{table}:{r.id}", r.data or {})
    by_code = defaultdict(dict)
    for (code, period, table), filings in reports.items():
        best = max(f[0] for f in filings)
        selected = [(ref, vals) for rank, ref, vals in filings if rank == best]
        originals = [(ref, vals) for ref, vals in selected if ref.startswith("raw.")]
        if originals and table != "cashflow_statement":
            selected = originals
        record = by_code[code].setdefault(period, {"fields": {}, "filings": {}})
        record["filings"][table] = [ref for ref, _ in selected]
        for key, aliases in INPUTS[table].items():
            field_value = None
            for name in aliases:
                observed = {number(vals[name]) for _, vals in selected if name in vals}
                if len(observed) == 1 and None not in observed:
                    field_value = observed.pop()
                    break
            record["fields"][key] = field_value
    return by_code


def quarter_id(day):
    return day.year * 4 + day.month // 3 - 1


def trend_values(facts, annual):
    fields = [k for group in INPUTS.values() for k in group]
    cumulative = {quarter_id(p): r["fields"] for p, r in facts.items()}
    values = {}
    for p in sorted(facts):
        if annual and p.month != 12:
            continue
        index = quarter_id(p)
        prev = cumulative.get(index-1, {})
        row = {}
        for key in fields:
            n = cumulative[index].get(key)
            if not annual and p.month != 3 and key not in INPUTS["balance_sheet"]:
                n = n-prev[key] if n is not None and prev.get(key) is not None else None
            row[key] = n / Decimal(100_000_000) if n is not None else None
        values[p] = row
    quarters = {quarter_id(p): r["revenue"] for p,r in values.items()}
    for p, row in values.items():
        revenue, cost, net = row["revenue"], row.pop("cost"), row.pop("netIncome")
        row["grossMargin"] = (revenue-cost)*100/revenue if revenue and cost is not None else None
        row["netMargin"] = net*100/revenue if revenue and net is not None else None
        last_four = [quarters.get(quarter_id(p)-i) for i in range(4)]
        row["revenueBasis"] = revenue if annual else (sum(last_four) if None not in last_four else None)
    return dict(sorted(values.items())[-8:])


def verified_calendars(session, start, end):
    calendars = defaultdict(dict)
    for exch, day, opened in session.execute(select(TradeCalendar.exchange, TradeCalendar.cal_date, TradeCalendar.is_open)
                                              .where(TradeCalendar.cal_date.between(start, end))):
        calendars[exch][day] = bool(opened)
    for contract in _contracts(session):
        left, right = map(date.fromisoformat, (contract["start"], contract["end"]))
        if right > end or right < start:
            continue
        days = _saved(session, contract)
        if days is None:
            continue
        opened = set(days)
        day = max(left, start)
        while day <= right:
            exists = calendars["SSE"].get(day, day in opened)
            calendars["SSE"][day] = exists if exists == (day in opened) else None
            day += timedelta(days=1)
    calendars["SZSE"] = calendars["SSE"] | calendars["SZSE"]
    return calendars


def valuation(security, data, calendar, suspended, end):
    start = date.fromisoformat(data["start"])
    days = {date.fromisoformat(d):(number(pe),number(pb)) for d,pe,pb in data["valuations"]}
    expected = {day for day, status in calendar.items() if status} - suspended
    gaps = expected - days.keys()
    reason = None
    if security.list_date is None or security.list_date > start:
        reason = "上市未满十年或缺上市日期"
    elif len(calendar) != (end-start).days + 1 or None in calendar.values():
        reason = "交易日历不完整"
    elif gaps or days.keys()-expected:
        reason = "开市与估值日期集合不匹配"
    elif end not in days:
        reason = "缺当日估值"
    evidence = {"listing":str(security.list_date), "exchange":security.exchange, "calendarDays":len(calendar),
                "windowDays":(end-start).days+1, "valuationDays":len(days), "missingKnownOpenDays":len(gaps),
                "suspendedDays":len(suspended)}
    for i,key in enumerate(("pePercentile10y", "pbPercentile10y")):
        nums = [pair[i] for pair in days.values()]
        issue = reason
        if not issue and (None in nums or days[end][i] <= 0):
            issue = "交易日缺值或当日倍数非正"
        positives = [n for n in nums if n is not None and n > 0]
        pct = None if issue else 100*sum(n <= days[end][i] for n in positives)/len(positives)
        evidence[key] = {"value":pct, "reason":issue}
    return evidence


def reconciled_debt(balance, indicator):
    parts = [number(balance.get(k)) for k in DEBT]
    if None in parts or min(parts) < 0:
        return None
    known = number(indicator.get("interestdebt"))
    fixed = sum(parts[:-1])
    if known is None:
        return fixed if parts[-1] == 0 else None
    return known if known in (fixed, fixed+parts[-1]) else None


def risk_results(data, card, distributions, annual):
    reports = data["reports"]
    balance_period = max(reports.get("balance",{}), default="")
    balance = reports.get("balance",{}).get(balance_period,{}).get("values",{})
    indicator = reports.get("indicator",{}).get(balance_period,{}).get("values",{})
    assets, money, goodwill = [number(balance.get(k)) for k in ("total_assets","money_cap","goodwill")]
    debt = reconciled_debt(balance, indicator)
    high = "unknown" if not assets or money is None or debt is None else (
        "warn" if money/assets > Decimal(".25") and debt/assets > Decimal(".35") else "normal")
    gw = None if goodwill is None or not assets else goodwill/assets
    goodwill_status = "unknown" if gw is None else "danger" if gw > Decimal(".35") else "warn" if gw > Decimal(".2") else "normal"
    last = max(annual)
    cash, profit = annual[last]["cashflow"], annual[last]["profit"]
    real = "unknown" if cash is None or not profit else "warn" if cash/profit < Decimal(".8") else "normal"
    fin = {r["period"]:r["values"] for r in card["financials"] if r["table"]=="balance" and r["kind"]=="cumulative"}
    latest = max(fin, default="")
    balances = fin.get(latest,{})
    inds = next((r["values"] for r in card["financials"] if r["table"]=="indicator"
                 and r["kind"]=="cumulative" and r["period"]==latest),{})
    net_debt = reconciled_debt(balances, inds)
    cap = number(balances.get("money_cap"))
    net = "unknown" if str(balances.get("comp_type")) in ("2","3","4") or cap is None or net_debt is None else (
        "warn" if cap-net_debt < 0 else "normal")
    dividend = {str(r["year"]):r for r in distributions}
    last_fy = max((p for table in reports.values() for p in table if p.endswith("-12-31")), default=None)
    financing, financing_inputs = [], {}
    if last_fy:
        for year in range(int(last_fy[:4])-2, int(last_fy[:4])+1):
            paid = dividend.get(str(year),{})
            amount = number(paid.get("cashAmount")) if paid.get("status")=="complete" else None
            flow = reports.get("cashflow",{}).get(f"{year}-12-31",{}).get("values",{})
            ocf = number(flow.get("n_cashflow_act"))
            loans = [number(flow.get(k)) for k in ("c_recp_borrow","proc_issue_bonds","c_recp_cap_contrib")]
            if amount == 0:
                status = "normal"
            elif amount is None or ocf is None:
                status = "unknown"
            elif ocf >= amount:
                status = "normal"
            elif any(n is not None and n>0 for n in loans):
                status = "danger"
            else:
                status = "unknown" if None in loans else "warn"
            financing.append(status)
            financing_inputs[year] = {"paid":str(amount) if amount is not None else None,
                                      "cashflow":flow, "status":status}
    combined = next((s for s in ("danger","warn","unknown") if s in financing), "normal" if financing else "unknown")
    statuses = dict(highCashHighDebt=high, goodwill=goodwill_status, dividendVsFinancing=combined, realMoney=real, netCash=net)
    inputs = {"balancePeriod":balance_period, "balance":balance, "indicator":indicator,
              "netCashPeriod":latest, "netCashFields":{k:balances.get(k) for k in ("comp_type","money_cap",*DEBT)},
              "netCashInterestDebt":inds.get("interestdebt"), "dividendYears":financing_inputs,
              "realMoneyPeriod":str(last)}
    return statuses, inputs


def inspect(factory, session):
    snap = session.get(PublishedSnapshot, "screener")
    end = snap.as_of.date()
    stocks = {r["symbol"]:r for r in snap.payload["stocks"]}
    codes = sorted(stocks)
    assert codes and len(codes) == len(snap.payload["stocks"])
    facts = collect(factory, session, codes, end)
    cards = load_metric_card_inputs(session, codes, end)
    risk = load_risk_inputs(session, codes, end, dividend_years={c:cards[c]["dividends"] for c in codes})
    names = {s.ts_code:s for s in session.scalars(select(Security).where(Security.ts_code.in_(codes)))}
    distributions = load_shareholder_returns(session, codes, end, card_inputs=cards)
    start = date.fromisoformat(risk[codes[0]]["start"])
    calendars = verified_calendars(session, start, end)
    suspended = defaultdict(set)
    suspensions = select(Suspension.ts_code, Suspension.trade_date, DailyPrice.id, DailyPrice.vol, DailyPrice.amount).outerjoin(
        DailyPrice, (DailyPrice.ts_code==Suspension.ts_code) & (DailyPrice.trade_date==Suspension.trade_date)).where(
        Suspension.ts_code.in_(codes), Suspension.suspend_type=="S", Suspension.trade_date.between(start,end))
    for code, day, row_id, vol, amount in session.execute(suspensions):
        present = [v for v in (vol,amount) if v is not None]
        if row_id is None or (present and all(v==0 for v in present)):
            suspended[code].add(day)
    summary, risks, coverage = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    details, differences = {}, []
    for code, stock in sorted(stocks.items()):
        item = {}
        for mode in ("annual","quarterly"):
            trend = trend_values(facts[code], mode=="annual")
            actual = stock["financialTrends"][mode]
            assert [d.isoformat() for d in trend] == [r["period"] for r in actual], (code,mode,"期间不同")
            rec = {}
            for (day, row), payload in zip(trend.items(), actual):
                assert set(payload)=={"period",*row}, (code,mode,"字段不同")
                for k,v in row.items():
                    p = payload[k]
                    ok = v is None and p is None or v is not None and p is not None and isclose(float(v),p,rel_tol=1e-9,abs_tol=1e-8)
                    summary[mode+"."+k]["bothNull" if v is None and ok else "equalValue" if ok else "different"] += 1
                    if not ok:
                        differences.append([code,mode,str(day),k,p,str(v)])
                rec[str(day)] = {k:float(v) if v is not None else None for k,v in row.items()}
            item[mode] = rec
        # 包含季同比和滚动四季分母引用的基期，源行 ID 可直接回到原件和核心表。
        item["filings"] = {str(p):r["filings"] for p,r in sorted(facts[code].items()) if p.year >= end.year-12}
        val = valuation(names[code], risk[code], calendars[names[code].exchange], suspended[code], end)
        for key in ("pePercentile10y","pbPercentile10y"):
            v = val[key]
            p = stock["researchValues"][key]
            ok = p is None and v["value"] is None or p is not None and v["value"] is not None and isclose(p,v["value"])
            if not ok or p is None and not stock["listMissingReasons"].get(key):
                differences.append([code,key,p,v])
            coverage[key][v["reason"] or "verifiedValue"] += 1
        item["valuation10y"] = val
        statuses, inputs = risk_results(risk[code], cards[code], distributions[code]["annualDistributions"],
                                        trend_values(facts[code],True))
        observed = stock["listRisks"]
        assert list(statuses)==[r["key"] for r in observed], code
        for r in observed:
            expected = statuses[r["key"]]
            if r["status"]!=expected or not r["period"] or not r["detail"]:
                differences.append([code,r["key"],r["status"],expected])
            risks[r["key"]][expected] += 1
        item["riskStatus"] = statuses
        item["riskInputs"] = inputs
        details[code] = item
    return dict(asOf=str(end),version=snap.version,stocks=len(codes),trends=summary,
                valuation10y=coverage, risks=risks, bySymbol=details, differences=differences)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        result = inspect(factory,session)
    args.output.write_text(json.dumps(result, ensure_ascii=False, separators=(",",":"), allow_nan=False)+"\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in ("bySymbol","differences")}, ensure_ascii=True))
    if result["differences"]:
        print(json.dumps(result["differences"][:10], ensure_ascii=True))
        raise SystemExit(1)
