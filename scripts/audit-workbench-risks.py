"""从原始风险输入重放现行规则，并逐证券对照接口、发布版本、持久化风险明细及历史策略。"""

from collections import Counter, defaultdict
import json
from decimal import Decimal
from math import ceil
from pathlib import Path
import sys

from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings
from app.db.models import RiskResult
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider
from app.stocks.derived.risk_inputs import load_risk_inputs
from app.stocks.derived.workbench_risks import calculate_risks
from app.stocks.service import WorkbenchService
from app.stocks.shareholder_returns import paid_cash_distribution
from datetime import date


def independent_statuses(source):
    reports = source["reports"]
    period = max(reports.get("balance", {}), default="")
    annual = max((p for table in reports.values() for p in table if p.endswith("-12-31")), default="")
    def read(table, key, point):
        raw = reports.get(table, {}).get(point, {}).get("values", {}).get(key)
        return None if raw is None else Decimal(str(raw))
    def ratio(a, b):
        return a / b if a is not None and b not in (None, 0) else None
    def levels(n, yellow, red, *, lower=False, strict=False):
        if n is None:
            return "unknown"
        for threshold, status in [(red, "risk"), (yellow, "watch")]:
            limit = Decimal(str(threshold))
            if (n < limit if lower else n > limit if strict else n >= limit):
                return status
        return "normal"
    def worst(statuses):
        return max(statuses, key=["normal","watch","unknown","risk"].index)
    def debt(p):
        keys = ("st_borr","lt_borr","st_bonds_payable","bond_payable","lease_liab","non_cur_liab_due_1y")
        parts = [read("balance", k, p) for k in keys]
        if None in parts or min(parts) < 0:
            return None
        fixed = sum(parts[:-1])
        reported = read("indicator", "interestdebt", p)
        if reported is None:
            return fixed if parts[-1] == 0 else None
        return reported if reported in (fixed, sum(parts)) else None
    def income(key):
        return read("income", key, annual)
    assets = read("balance","total_assets",period)
    cash = ratio(read("balance","money_cap",period), assets)
    burden = ratio(debt(period), assets)
    result = {"high_cash_high_debt": "unknown" if None in (cash,burden) else
              ("risk" if cash > Decimal(".25") and burden > Decimal(".35") else "normal"),
              "goodwill_risk": levels(ratio(read("balance","goodwill",period),assets),.2,.35,strict=True)}
    profit = income("n_income_attr_p")
    parts = [income(k) for k in ("invest_income","fv_value_chg_gain","non_oper_income","non_oper_exp")]
    total = sum(parts[:3]) - parts[3] if None not in parts else None
    positive = profit if profit and profit > 0 else None
    result["profit_structure_non_operating_dependency"] = levels(ratio(total, positive),.3,.5)
    assets_loss, credit_loss = income("assets_impair_loss"), income("credit_impa_loss")
    loss = None if assets_loss is None else -min(0,assets_loss)-min(0,credit_loss or 0)
    result["impairment_pressure"] = levels(ratio(loss,positive),.3,.5)
    comp = income("t_compr_income")
    gap = abs((comp-profit)/profit) if comp is not None and profit else None
    result["comprehensive_income_gap"] = levels(gap,.2,.4)
    basic, adj = income("basic_eps"), read("indicator","verified_adjusted_eps",annual)
    eps = (basic-adj)/abs(basic) if basic and adj is not None else None
    result["eps_quality_gap"] = levels(eps,.15,.30)
    prices = source["valuations"]
    high = 0
    status = "normal" if prices and source.get("windowVerified") and not source.get("valuationIssues") else "unknown"
    for index in (1,2):
        series = sorted(Decimal(str(row[index])) for row in prices if row[index] is not None and Decimal(str(row[index]))>0)
        last = Decimal(str(prices[-1][index])) if prices and prices[-1][index] is not None else None
        if not series or last is None or last <= 0:
            status = "unknown"
        else:
            high += last > series[ceil(.3*len(series))-1] and last >= series[ceil(.7*len(series))-1]
    result["valuation_band"] = status if status == "unknown" else ["normal","watch","risk"][high]
    annual_ids = ["interest_debt_burden","equity_cash_support","earnings_quality_non_recurring",
                  "dividend_vs_financing","dividend_sustainability"]
    if not annual:
        return result | dict.fromkeys(annual_ids,"unknown")
    year = int(annual[:4])
    prior_debt, end_debt = debt(f"{year-1}-12-31"), debt(annual)
    average = None if None in (prior_debt,end_debt) else (prior_debt+end_debt)/2
    interest = income("fin_exp_int_exp")
    result["interest_debt_burden"] = worst([levels(ratio(income("ebit"),interest),2,1,lower=True),
                                             levels(ratio(interest,average),.1,.2)])
    flows = [read("cashflow","n_cashflow_act",f"{y}-12-31") for y in range(max(2015, year-9),year+1)]
    total = sum(flows) if None not in flows else None
    result["equity_cash_support"] = levels(ratio(total,read("balance","total_hldr_eqy_inc_min_int",annual)),1,.5,lower=True)
    funding_status, quality, dividends, last_flows = [], [], [], []
    for y in range(year-2,year+1):
        p = f"{y}-12-31"
        flow = read("cashflow","n_cashflow_act",p)
        last_flows.append(flow)
        net, adjusted = read("income","n_income_attr_p",p), read("indicator","verified_adjusted_profit",p)
        quality.append(levels(ratio(flow,adjusted),1,.8,lower=True))
        quality.append(levels((net-adjusted)/net if net and adjusted is not None else None,.3,.5))
        paid = paid_cash_distribution(source["dividends"].get(str(y),{}), date.fromisoformat(source["asOf"]))["cashAmount"]
        dividends.append(paid)
        covered = ratio(flow,paid)
        funded = any((read("cashflow", k, p) or 0) > 0 for k in ("c_recp_borrow","proc_issue_bonds","c_recp_cap_contrib"))
        if paid == 0 and flow is not None:
            funding_status.append("normal")
        else:
            funding_status.append("unknown" if covered is None else "normal" if covered >= 1 else "risk" if funded else "watch")
    result["earnings_quality_non_recurring"] = worst(quality)
    result["dividend_vs_financing"] = worst(funding_status)
    total_paid = None if None in dividends else sum(dividends)
    cash_flow = None if None in last_flows else sum(last_flows)
    result["dividend_sustainability"] = ("normal" if total_paid == 0 and cash_flow is not None else
                                         levels(ratio(cash_flow,total_paid),1.2,1,lower=True))
    return result


def main():
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        repo = SnapshotRepository(factory)
        service = WorkbenchService(StocksDatabaseProvider(repo, factory))
        products = session.execute(text("""
            select product, (as_of at time zone 'UTC')::date as day, version from mart.published_snapshot
            where product like 'stock-workbench:%' order by product
        """)).all()
        groups = defaultdict(dict)
        for product, day, version in products:
            groups[day][product.split(":", 1)[1]] = version
        assert groups
        totals = defaultdict(Counter)
        evidence = {}
        for day, versions in groups.items():
            sources = load_risk_inputs(session, list(versions), day)
            records = defaultdict(dict)
            for row in session.scalars(select(RiskResult).where(RiskResult.ts_code.in_(versions),
                                                                   RiskResult.as_of == day)):
                records[row.ts_code][row.risk_id] = row
            for symbol, version in versions.items():
                result = service.get_workbench(symbol)
                current = result.data["risks"]
                rebuilt = calculate_risks(sources[symbol])
                assert result.meta.version == version and current == rebuilt, symbol
                items = [item for group in current["groups"] for item in group["items"]]
                measured = independent_statuses(sources[symbol])
                assert measured == {i["id"]: i["status"] for i in items}, symbol
                assert len(items) == len(records[symbol]) == 12
                statuses = Counter(item["status"] for item in items)
                score = max(0, 100 - 18*statuses["risk"] - 10*statuses["watch"] - 4*statuses["unknown"])
                assert current["score"] == (score if statuses["unknown"] <= 4 else None)
                assert current["assessment"]["knownCount"] == 12 - statuses["unknown"]
                for item in items:
                    key = item["id"]
                    record = records[symbol][key]
                    assert (record.snapshot_version, record.rule_version, record.inputs, record.status,
                            record.missing_fields, record.conclusion, record.formula) == (
                            version, current["ruleVersion"], item, item["status"], item["missingFields"],
                            item["conclusion"], item["formula"])
                    totals[key][item["status"]] += 1
                evidence[symbol] = {"version": version, "asOf": day.isoformat(), "ruleVersion": current["ruleVersion"],
                                    "score": current["score"], "level": current["level"],
                                    "status": {item["id"]: item["status"] for item in items}}
        history = session.execute(text("""
            select count(*) as versions, count(*) filter (where payload->'risks' is not null) as with_risks
            from mart.page_snapshot where product like 'stock-workbench:%'
        """)).mappings().one()
    print(json.dumps({"stocks": evidence, "coverageByRule": totals, "historicalPageRows": dict(history),
                      "independentlyGraded": sum(sum(counts.values()) for counts in totals.values())},
                     ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
