from __future__ import annotations

from dataclasses import dataclass,field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from functools import lru_cache
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.content.api.read_models import WorkbenchContent, load_workbench_content
from app.shared.trading_days import load_trading_days
from app.db.models import DailyBasic, DailyPrice, MetricValue, PublishedSnapshot, RiskResult, Security, StockDailyChart
from app.stocks.derived.inputs import CoreSnapshot, FinancialPoint, visible_financial_rows
from app.stocks.derived.registry import FORMULAS
from app.stocks.derived.risk_assessment import assess_risk_checklist
from app.marts.contracts import MartCandidate, MartRows
from app.stocks.payload_validation import screener_bootstrap
from app.stocks.screener_contracts import SCREENER_FIELDS
from app.stocks.marts.prices import AdjustmentIndex, adjusted_value, build_adjustment_index
from app.shared.boards import normalize_board
from app.shared.stock_mapping import StockMappingPolicy, classification_fields
from app.stocks.research import financial_research
from app.stocks.return_history import project_screener_roic
from app.stocks.marts.screener_list import list_projection, valuation_windows
from app.stocks.workbench_enrichment import load_holders,valuation_bands


@dataclass(frozen=True)
class BuildResult:
    version: str
    as_of: date
    rows: tuple[dict[str, Any], ...]
    payload: dict[str, Any]


@dataclass(frozen=True)
class DerivedMartRows:
    metrics: dict[str, tuple[MetricValue, ...]]
    risks: dict[str, tuple[RiskResult, ...]]


@dataclass(frozen=True)
class WorkbenchInputs:
    sidebar: dict[str, object]
    securities: Mapping[str, Security]
    prices: Mapping[str, tuple[DailyPrice, ...]]
    latest_basics: Mapping[str, DailyBasic]
    financial_points: Mapping[str, tuple[FinancialPoint, ...]]
    adjustment_index: AdjustmentIndex
    basics: Mapping[str, tuple] = field(default_factory=dict)
    holders: Mapping[str, list] = field(default_factory=dict)
    statement_tables: Mapping[str, list] = field(default_factory=dict)
    company_projections: Mapping[str, dict] = field(default_factory=dict)
    return_histories: Mapping[str, dict] = field(default_factory=dict)
    business_histories: Mapping[str, dict] = field(default_factory=dict)
    forecast_charts: Mapping[str, dict] = field(default_factory=dict)
    content: Mapping[str, WorkbenchContent] = field(default_factory=dict)


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row.get(key) for key in SCREENER_FIELDS if key in row}


def _number(value: object) -> int | float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _as_of_datetime(as_of: date) -> datetime:
    return datetime.combine(as_of, time.min, timezone.utc)


def _latest(rows: Iterable[object], field: str) -> object | None:
    return max(rows, key=lambda row: getattr(row, field), default=None)


def _board(security: Security) -> str | None:
    data = security.data or {}
    value = security.board if security.board is not None else data.get("board")
    return normalize_board(value, exchange=security.exchange, symbol=security.ts_code)


def load_derived_mart_rows(
    session_factory: sessionmaker[Session] | None,
    symbols: Iterable[str],
    as_of: date,
) -> DerivedMartRows:
    selected = tuple(sorted(set(symbols)))
    if session_factory is None:
        return DerivedMartRows({}, {})
    if not selected:
        return DerivedMartRows({}, {})
    with session_factory() as session:
        metric_rows = session.scalars(
            select(MetricValue).where(
                MetricValue.ts_code.in_(selected),
                MetricValue.as_of == as_of,
                or_(
                    *(
                        and_(
                            MetricValue.metric_id == metric_id,
                            MetricValue.formula_version == definition.version,
                        )
                        for metric_id, definition in FORMULAS.items()
                    )
                ),
            ).order_by(
                MetricValue.ts_code,
                MetricValue.metric_id,
                MetricValue.formula_version,
            )
        ).all()
        risk_rows = session.scalars(
            select(RiskResult).join(PublishedSnapshot, and_(
                PublishedSnapshot.product == "stock-workbench:" + RiskResult.ts_code,
                PublishedSnapshot.version == RiskResult.snapshot_version)).where(
                RiskResult.ts_code.in_(selected),
                RiskResult.as_of == as_of,
            ).order_by(RiskResult.ts_code, RiskResult.risk_id)
        ).all()
    metrics: dict[str, list[MetricValue]] = {}
    risks: dict[str, list[RiskResult]] = {}
    for row in metric_rows:
        metrics.setdefault(row.ts_code, []).append(row)
    for row in risk_rows:
        risks.setdefault(row.ts_code, []).append(row)
    return DerivedMartRows(
        {symbol: tuple(rows) for symbol, rows in metrics.items()},
        {symbol: tuple(rows) for symbol, rows in risks.items()},
    )


def _latest_basic(snapshot: CoreSnapshot, symbol: str) -> DailyBasic | None:
    return _latest(
        (row for row in snapshot.daily_basics if row.ts_code == symbol),
        "trade_date",
    )  # type: ignore[return-value]


def _sidebar(
    snapshot: CoreSnapshot,
    securities: Iterable[Security] | None = None,
) -> dict[str, object]:
    latest_by_symbol: dict[str, DailyPrice] = {}
    for row in snapshot.daily_prices:
        previous = latest_by_symbol.get(row.ts_code)
        if previous is None or row.trade_date > previous.trade_date:
            latest_by_symbol[row.ts_code] = row
    rows: list[dict[str, object]] = []
    for security in securities if securities is not None else snapshot.securities:
        latest = latest_by_symbol.get(security.ts_code)
        if latest is None or getattr(latest, "close", None) is None:
            continue
        rows.append({
            "symbol": security.ts_code,
            "name": security.name,
            "pinyin": str((security.data or {}).get("pinyin", security.symbol)).lower(),
            "price": _number(getattr(latest, "close", None)),
            "changePercent": _number(getattr(latest, "pct_chg", None)),
            "mentions": 0,
        })
    return {"referenceDate": snapshot.as_of.isoformat(), "events": [], "stocks": rows}


def prepare_workbench_inputs(snapshot: CoreSnapshot, session_factory=None) -> WorkbenchInputs:
    from app.stocks.marts.shareholder_returns import json_numbers
    from app.stocks.financial_runtime import tables_for_build
    from app.stocks.shareholder_supply import load_company_projections
    from app.stocks.return_history import return_history,disclosure_diagnostics
    from app.stocks.business_projection import load_business_histories
    securities = {row.ts_code: row for row in snapshot.securities}
    prices: dict[str, list[DailyPrice]] = {}
    for row in snapshot.daily_prices:
        prices.setdefault(row.ts_code, []).append(row)
    basics: dict[str, DailyBasic] = {}
    all_basics = {}
    for row in snapshot.daily_basics:
        all_basics.setdefault(row.ts_code,[]).append(row)
        previous = basics.get(row.ts_code)
        if previous is None or row.trade_date > previous.trade_date:
            basics[row.ts_code] = row
    raw_financial: dict[str, dict[str, list[FinancialPoint]]] = {}
    for row in snapshot.financial_points:
        if row.ts_code and row.source_table:
            raw_financial.setdefault(row.ts_code, {}).setdefault(
                row.source_table,
                [],
            ).append(row)
    financial = {
        symbol: tuple(
            sorted(
                (
                    row
                    for source_rows in by_source.values()
                    for row in visible_financial_rows(source_rows, snapshot.as_of)
                ),
                key=lambda row: (
                    row.end_date,
                    row.source_table or "",
                    row.report_type or "",
                ),
            )
        )
        for symbol, by_source in raw_financial.items()
    }
    return_points=(*snapshot.financial_points,*disclosure_diagnostics(session_factory,tuple(securities),snapshot.as_of))
    forecast_data = {}
    content = {}
    if session_factory is not None:
        from app.stocks.forecast_events import forecast_charts
        with session_factory() as s:
            price_dates = {symbol: [r.trade_date for r in rows if None not in (r.open, r.high, r.low, r.close)]
                           for symbol, rows in prices.items()}
            trading_days = load_trading_days(s, price_dates)
            forecast_data = forecast_charts(s, price_dates, trading_days=trading_days)
            content = load_workbench_content(s, securities, snapshot.as_of, trading_days)
    return WorkbenchInputs(
        sidebar=_sidebar(snapshot, securities.values()),
        securities=securities,
        prices={symbol: tuple(rows) for symbol, rows in prices.items()},
        latest_basics=basics,
        financial_points=financial,
        adjustment_index=build_adjustment_index(snapshot),
        basics={code:tuple(rows) for code,rows in all_basics.items()},
        holders=load_holders(session_factory,tuple(securities),snapshot.as_of),
        statement_tables=tables_for_build(session_factory,snapshot.as_of,tuple(securities)),
        company_projections=json_numbers(load_company_projections(session_factory,tuple(securities),snapshot.as_of,basics=all_basics,financial={code:tuple(p for p in snapshot.financial_points if p.ts_code==code) for code in securities})),
        return_histories={code:return_history([p for p in return_points if p.ts_code==code],snapshot.as_of) for code in securities},
        business_histories=load_business_histories(session_factory,tuple(securities),snapshot.as_of),
        forecast_charts=forecast_data,
        content=content,
    )


def _quote(
    snapshot: CoreSnapshot,
    security: Security,
    latest: object,
    basic: DailyBasic | None = None,
) -> dict[str, object]:
    basic = basic or _latest_basic(snapshot, security.ts_code)
    close = _number(getattr(latest, "close", None))
    previous = _number(getattr(latest, "pre_close", None))
    if previous is None and close is not None:
        change = _number(getattr(latest, "pct_chg", None))
        previous = close / (1 + change / 100) if change not in {None, -100} else close
    data = security.data or {}
    categories, _ = classification_fields(snapshot, security)
    return {
        "symbol": security.ts_code,
        "name": security.name,
        "market": security.exchange or "",
        "industry": categories["industry"],
        "subIndustry": categories["subIndustry"],
        "mentions": 0,
        "latestMention": "",
        "concepts": [],
        "price": close,
        "marketCap": None if basic is None or _number(basic.total_mv) is None else _number(basic.total_mv) / 10000,
        "changePercent": _number(getattr(latest, "pct_chg", None)),
        "pe": _number(getattr(basic, "pe", None)),
        "peTtm": _number((getattr(basic, "data", None) or {}).get("pe_ttm")),
        "pb": _number(getattr(basic, "pb", None)),
        "previousClose": previous,
        "intraday": {"day": [], "fiveDay": []},
    }


@lru_cache(maxsize=1)
def _ratio_field_specs():
    path = Path(__file__).resolve().parents[4] / "config" / "stock-ratio-catalog.json"
    return json.loads(path.read_text(encoding="utf-8"))["fields"]


def _visible_statement_points(
    snapshot: CoreSnapshot,
    symbol: str,
) -> list[FinancialPoint]:
    by_source: dict[str, list[FinancialPoint]] = {}
    for row in snapshot.financial_points:
        if row.ts_code == symbol and row.source_table:
            by_source.setdefault(row.source_table, []).append(row)
    return sorted(
        (
            row
            for rows in by_source.values()
            for row in visible_financial_rows(rows, snapshot.as_of)
        ),
        key=lambda row: (
            row.end_date,
            row.source_table or "",
            row.report_type or "",
        ),
    )


def _metric_overview(
    snapshot: CoreSnapshot,
    symbol: str,
    metrics: tuple[MetricValue, ...],
    points: list[FinancialPoint] | None = None,
) -> dict[str, object]:
    points = points if points is not None else _visible_statement_points(snapshot, symbol)
    balance = max((row for row in points if row.source_table == "core.balance_sheet" and row.report_type == "1"),
                  key=lambda row: row.end_date, default=None)
    sources = {
        "income": "core.income_statement", "balance": "core.balance_sheet",
        "cashflow": "core.cashflow_statement",
    }
    annual = {
        (row.source_table, row.end_date.isoformat()): row.values for row in points
        if row.end_date.month == 12 and row.end_date.day == 31
        and row.end_date <= snapshot.as_of and row.source_table in sources.values()
        and row.report_type == "1"
    }
    periods = sorted({period for _, period in annual})
    fields = []
    for spec in _ratio_field_specs():
        if "table" not in spec:
            continue
        values = []
        for period in periods:
            row = annual.get((sources[spec["table"]], period), {})
            value = next((_number(row[key]) for key in spec["keys"] if _number(row.get(key)) is not None), None)
            values.append(float(value) / 1e8 if value is not None else None)
        fields.append({"id": spec["id"], "label": spec["label"], "category": spec["category"], "values": values})
    cards = [
        {"label": row.metric_id, "value": "--" if row.value is None else format(row.value, "f")}
        for row in metrics
    ]
    if balance:
        debt_keys = ("st_borr", "lt_borr", "st_bonds_payable", "non_cur_liab_due_1y", "bond_payable", "lease_liab")
        debts = [_number(balance.values.get(key)) for key in debt_keys]
        cash = _number(balance.values.get("money_cap"))
        if cash is not None and all(value is not None for value in debts):
            debt = sum(debts)
            cards.append({"label": "账上净现金", "value": f"{(cash - debt) / 1e8:.2f} 亿元",
                          "detail": f"{balance.end_date} 合并资产负债表：货币资金 {cash / 1e8:.2f} 亿元 − 有息负债 {debt / 1e8:.2f} 亿元。"})
    return {
        "cards": cards,
        "years": periods,
        "fields": fields,
        "presets": [
            {"id": "cash-profit", "label": "真钱指数", "numerator": "operatingCashFlow", "denominator": "parentNetProfit", "unit": "multiple"},
            {"id": "cash-revenue", "label": "经现/营收", "numerator": "operatingCashFlow", "denominator": "revenue", "unit": "percent"},
        ],
    }


_RISK_TITLES = {
    "inventoryRisk": "存货风险", "goodwillRisk": "商誉/资产",
    "netDebtRisk": "净负债风险", "cashProfitRisk": "现金利润支撑",
    "valuationRisk": "估值分位带", "dividendRisk": "分红可持续性",
    "cashDividendRisk": "现金流覆盖分红",
}


def _risk_checklist(risks: tuple[RiskResult, ...]) -> dict[str, object]:
    from app.stocks.derived.workbench_risks import RULES, VERSION
    if any(r.rule_version == VERSION for r in risks):
        by_id = {r.risk_id: r.inputs for r in risks if r.rule_version == VERSION}
        return assess_risk_checklist(dict(ruleVersion=VERSION, groups=[
            dict(id=group, title=title, description=description, items=[by_id[key] for key in keys if key in by_id])
            for group, title, description, keys in (
                ("operations", "经营风险", "资产质量、利润结构与现金流支撑", list(RULES)[:7]),
                ("dividend", "分红安全垫", "估值、负债与分红可持续性", list(RULES)[7:]))]))
    groups = {"operations": [], "dividend": []}
    for row in risks:
        group = "dividend" if row.risk_id in {"valuationRisk", "dividendRisk", "cashDividendRisk"} else "operations"
        groups[group].append({
            "id": row.risk_id,
            "title": _RISK_TITLES.get(row.risk_id, row.risk_id),
            "summary": row.conclusion,
            "detail": row.conclusion,
            "status": row.status,
            "reportDate": row.as_of.isoformat(),
            "announcementDate": "",
            "reportType": "",
            "inputs": [{"label": str(key), "value": str(value)} for key, value in sorted((row.inputs or {}).items())],
            "formula": row.formula,
            "conclusion": row.conclusion,
            "missingFields": list(row.missing_fields or []),
        })
    return assess_risk_checklist({
        "groups": [
            {"id": "operations", "title": "经营风险", "description": "资产质量、利润结构与现金流支撑", "items": groups["operations"]},
            {"id": "dividend", "title": "分红安全垫", "description": "估值、负债与分红可持续性", "items": groups["dividend"]},
        ],
    })


def _financial_workspace(
    snapshot: CoreSnapshot,
    symbol: str,
    points: list[FinancialPoint] | None = None,
) -> dict[str, object]:
    points = points if points is not None else _visible_statement_points(snapshot, symbol)
    periods = sorted({row.end_date.isoformat() for row in points})
    latest_visible = max((row.visible_at for row in points if row.visible_at), default=None)
    annual = financial_research(points)['financialTrends']['annual']
    def trend(label, field, color):
        return {'label':label,'color':color,'points':[{'period':r['period'],'value':r[field]} for r in annual if r[field] is not None]}
    table_specs = {
        "core.income_statement": ("income", "利润表"),
        "core.balance_sheet": ("balance", "资产负债表"),
        "core.cashflow_statement": ("cashflow", "现金流量表"),
    }
    tables: list[dict[str, object]] = []
    for source, (kind, label) in table_specs.items():
        source_points = [row for row in points if row.source_table == source]
        source_periods = sorted({row.end_date.isoformat() for row in source_points})
        field_names = sorted({str(key) for row in source_points for key, value in row.values.items() if _number(value) is not None})
        if kind == "cashflow":
            from app.stocks.financial_supply import META
            field_names = sorted({key for row in source_points for key in row.values
                                  if key not in META and key != "sourceId" and not key.startswith("_")})
        rows = []
        for name in field_names:
            values = []
            for period in source_periods:
                point = next(row for row in source_points if row.end_date.isoformat() == period)
                values.append(_number(point.values.get(name)))
            rows.append({"label": name, "values": values, "yoy": [], "qoq": []})
        if source_points:
            metadata = []
            for period in source_periods:
                record = next(row for row in source_points if row.end_date.isoformat() == period)
                metadata.append({
                    "period": period, "reportType": record.report_type or "",
                    "announcementDate": record.visible_at.isoformat() if record.visible_at else "",
                    "sourceIds": [record.source_record_key] if record.source_record_key else [],
                })
                if kind == "balance" and record.values.get("comp_type"):
                    metadata[-1]["companyType"] = str(record.values["comp_type"])
                if kind == "cashflow":
                    metadata[-1]["requestedFields"] = [key for key in field_names if key in record.values]
            tables.append({"kind": kind, "label": label, "periods": source_periods, "rows": rows,
                           "amountUnit": "yuan", "reportMetadata": metadata})
    return {
        "years": periods,
        "reportTypes": sorted({str(row.report_type) for row in points if row.report_type}),
        "announcementDate": latest_visible.isoformat() if latest_visible else "",
        "dashboard": {
            "products": [], "productTrend": [], "bands": [],
            "peg": {"value": None, "pe": None, "growth": None},
            "trends": [
                {'id':'profit','title':'营收 / 净利润趋势','series':[trend('营业收入','revenue','#0ea5e9'),trend('归母净利润','profit','#10b981')]},
                {'id':'cash','title':'经营现金流趋势','series':[trend('经营现金流','cashflow','#0ea5e9')]},
            ], "dcf": {"available": False, "message": "由可见年报与明确假设在研究面板计算"},
        },
        "shareholders": {
            "holders": [], "dividendSeries": [], "returnSeries": [],
            "concentrationSeries": [], "northboundSeries": [], "executiveTrades": [],
        },
        "statements": {"diagnosis": [], "dupont": [], "operations": [], "sankey": {"inflows": [], "outflows": []}, "tables": tables},
    }


def _stock_row(
    snapshot: CoreSnapshot,
    security: Security,
    derived_rows: DerivedMartRows,
    price: DailyPrice | None,
    basic: DailyBasic | None,
    categories: Mapping[str, object],
    research: dict | None = None,
) -> dict[str, object]:
    inputs = snapshot.security_inputs.get(security.ts_code)
    values = inputs.values if inputs else {}
    metrics = {
        row.metric_id: _number(row.value)
        for row in derived_rows.metrics.get(security.ts_code, ())
    }
    # 选股筛选的既有口径独立于工作台发布，按当期输入重算。
    screening = inputs.risk_values if inputs else {}
    inventories = _number(screening.get("inventory_ratio"))
    debt_ratio = _number(screening.get("net_debt_ratio"))
    data = security.data or {}
    list_years = 0.0
    if security.list_date:
        list_years = round((snapshot.as_of - security.list_date).days / 365.25, 1)
    profit = values.get("profit")
    # Core daily_basic.total_mv uses Tushare 万元; the screener contract uses 亿元.
    total_mv = _number(getattr(basic, "total_mv", None))
    return {
        **(research or {}),
        "symbol": security.ts_code,
        "name": security.name,
        "price": _number(getattr(price, "close", None)),
        "change": _number(getattr(price, "pct_chg", None)),
        "marketCap": None if total_mv is None else total_mv / 10000,
        "pe": _number(getattr(basic, "pe", None)),
        "pb": _number(getattr(basic, "pb", None)),
        "dividendYield": _number(getattr(basic, "dv_ttm", None) or getattr(basic, "dv_ratio", None)),
        "roe": _number(values.get("roe")),
        "grossMargin": _number(values.get("grossprofit_margin")),
        "netMargin": _number(values.get("netprofit_margin")),
        "revenueGrowth": metrics.get("revenueGrowth"),
        "profitGrowth": metrics.get("profitGrowth"),
        "realMoneyIndex": (research or {}).get("researchValues", {}).get("realMoneyIndex"),
        "cashflowGrowth": metrics.get("cashflowGrowth"),
        "dividendYears": metrics.get("dividendYears"),
        "payoutRatio": metrics.get("payoutRatio"),
        "incomeSafety": metrics.get("realMoneyIndex"),
        "industry": categories["industry"],
        "board": _board(security) or "",
        "isST": bool(security.is_st),
        "isLoss": None if _number(profit) is None else _number(profit) < 0,
        "listingYears": list_years,
        "pePercentile": metrics.get("pePercentile"),
        "pbPercentile": metrics.get("pbPercentile"),
        "expectedDividendYield": metrics.get("expectedDividendYield"),
        "capitalReturnRatio": metrics.get("capitalReturnRatio"),
        "roic": None,  # 下方使用同一可见年报序列投影已披露百分数。
        "institutionHolding": metrics.get("institutionHolding"),
        "majorIndustry": categories["majorIndustry"],
        "subIndustry": categories["subIndustry"],
        "concepts": list(data.get("concepts", [])) if isinstance(data.get("concepts", []), (list, tuple)) else [],
        "region": categories["region"],
        "inventoryRisk": None if inventories is None else inventories >= 0.4,
        "netDebt": None if debt_ratio is None else debt_ratio >= 1,
        "metricBasis": {"dividendYield": "daily-basic-yield-not-verified-FY", "payoutRatio": "latest-report-not-three-FY-average", "realMoneyIndex": "annual-OCF/parent-profit", "capitalReturnRatio": "existing-published-scope-not-verified-FY"},
    }


class ScreenerMartBuilder:
    def __init__(
        self,
        source: sessionmaker[Session] | Iterable[dict[str, Any]] | None = None,
        publisher: object | None = None,
    ) -> None:
        del publisher
        self._session_factory = source if callable(source) else None
        self._rows = tuple(source) if source is not None and not callable(source) else ()

    def build(
        self,
        snapshot_or_as_of: CoreSnapshot | date,
        version: str,
        rows: Iterable[dict[str, Any]] | None = None,
        derived_rows: DerivedMartRows | None = None,
    ) -> MartCandidate | BuildResult:
        if isinstance(snapshot_or_as_of, date) and not isinstance(snapshot_or_as_of, CoreSnapshot):
            selected = tuple(_clean_row(row) for row in (rows if rows is not None else self._rows))
            bootstrap = screener_bootstrap(selected)
            return BuildResult(version, snapshot_or_as_of, selected, {"bootstrap": bootstrap, "stocks": list(selected)})
        snapshot = snapshot_or_as_of
        mapping = StockMappingPolicy.load()
        categories = {security.ts_code: classification_fields(snapshot, security, mapping)
            for security in snapshot.securities}
        loaded = derived_rows or load_derived_mart_rows(
            self._session_factory,
            (security.ts_code for security in snapshot.securities),
            snapshot.as_of,
        )
        latest_prices: dict[str, DailyPrice] = {}
        for price in snapshot.daily_prices:
            previous = latest_prices.get(price.ts_code)
            if previous is None or price.trade_date > previous.trade_date:
                latest_prices[price.ts_code] = price
        latest_basics: dict[str, DailyBasic] = {}
        for basic in snapshot.daily_basics:
            previous = latest_basics.get(basic.ts_code)
            if previous is None or basic.trade_date > previous.trade_date:
                latest_basics[basic.ts_code] = basic
        prepared = prepare_workbench_inputs(snapshot,self._session_factory)
        research_inputs = prepared.financial_points
        selected = tuple(
            row
            for security in snapshot.securities
            for row in (
                _stock_row(
                    snapshot,
                    security,
                    loaded,
                    latest_prices.get(security.ts_code),
                    latest_basics.get(security.ts_code),
                    categories[security.ts_code][0],
                    financial_research(research_inputs.get(security.ts_code, ()),
                                       tables=prepared.statement_tables.get(security.ts_code, ()), as_of=snapshot.as_of),
                ),
            )
            if row.get("price") is not None
        )
        shareholder_reports, risk_inputs, card_inputs = {}, {}, {}
        if self._session_factory and selected:
            from app.stocks.shareholder_returns import load_shareholder_returns
            from app.stocks.derived.inputs import load_metric_card_inputs
            from app.stocks.derived.risk_inputs import load_risk_inputs
            codes = [r['symbol'] for r in selected]
            with self._session_factory() as session:
                card_inputs = load_metric_card_inputs(session, codes, snapshot.as_of)
                shareholder_reports = load_shareholder_returns(session, codes, snapshot.as_of, card_inputs=card_inputs)
                risk_inputs = load_risk_inputs(session, codes, snapshot.as_of,
                                              dividend_years={c:i['dividends'] for c,i in card_inputs.items()})
                valuation_windows(session, prepared.securities, risk_inputs, snapshot.as_of)
        for row in selected:
            company=prepared.company_projections.get(row['symbol'],{})
            row.setdefault('researchValues',{}).update(company.get('researchValues',{}))
            latest_holders=prepared.holders.get(row['symbol'],[])
            counts = next((s['points'] for s in company.get('concentrationSeries', []) if s['label'] == '股东户数'), [])
            returns = prepared.return_histories.get(row['symbol'], {}).get('reports', [])
            row['listHistory'] = {
                'holders': counts[-8:],
                'institutions': [{'period': h['reportDate'], 'value': h['institutionHolding']} for h in latest_holders[-8:]],
                'returns': [{k: r.get(k) for k in ('period','frequency','roe','roic')} for r in returns],
                'dividends': [{k: r.get(k) for k in ('year','cashAmount','parentProfit','payoutRatio','complete')} for r in company.get('annualReturns', [])
                              if snapshot.as_of.year - 3 <= int(r['year']) < snapshot.as_of.year],
            }
            if row['symbol'] in shareholder_reports:
                reports = shareholder_reports[row['symbol']]
                row['listHistory']['holders'] = [
                    {'period':p['period'], 'value':_number(p['holderCount'])}
                    for p in reports['concentrationPeriods'][-8:]]
            for key in ('pePercentile','pbPercentile'):
                row[key]=company.get('researchValues',{}).get(key)
            if latest_holders:
                row['institutionHolding']=latest_holders[-1]['institutionHolding']
                row['researchValues']['institutionHolding']=row['institutionHolding']
                pair=latest_holders[-2:]
                row['researchValues']['institutionHoldingChange']=pair[1]['institutionHolding']-pair[0]['institutionHolding'] if len(pair)==2 and all(p['institutionHolding'] is not None for p in pair) else None
            row['metricBasis'].update(pePercentile='five-year-complete-window-required',pbPercentile='five-year-complete-window-required',institutionHolding='top10-identified-total-capital-partial')
            code = row['symbol']
            list_projection(row, risk_inputs.get(code, {'reports':{}, 'dividends':{}}), card_inputs.get(code),
                            shareholder_reports.get(code, {}).get('annualDistributions', []), snapshot.as_of)
            project_screener_roic(row)
        payload = {
            "bootstrap": screener_bootstrap(selected),
            "stocks": list(selected),
        }
        issues = tuple(issue for row in selected for issue in categories[row["symbol"]][1])
        return MartCandidate("screener", _as_of_datetime(snapshot.as_of), payload, MartRows(screener=selected), snapshot.missing_data + issues)


class StockWorkbenchMartBuilder:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        publisher: object | None = None,
    ) -> None:
        del publisher
        self._session_factory = session_factory

    def build(
        self,
        snapshot_or_symbol: CoreSnapshot | str,
        symbol_or_as_of: str | date,
        version: str,
        derived_rows: DerivedMartRows | None = None,
        prepared_inputs: WorkbenchInputs | None = None,
        **legacy: object,
    ) -> MartCandidate | dict[str, Any]:
        if isinstance(snapshot_or_symbol, str):
            symbol = snapshot_or_symbol
            return {
                "symbol": symbol,
                "sidebar": legacy.get("sidebar") or {},
                "quote": legacy.get("quote") or {"symbol": symbol},
                "dailyChart": list(legacy.get("chart", ())),
                "metrics": legacy.get("metrics") or {},
                "risks": legacy.get("risks") or [],
                "financial": legacy.get("financial") or {},
                "isFallbackSample": False,
            }
        snapshot = snapshot_or_symbol
        symbol = str(symbol_or_as_of)
        prepared = prepared_inputs or prepare_workbench_inputs(snapshot,self._session_factory)
        security = prepared.securities.get(symbol)
        if security is None:
            raise ValueError(f"unknown security: {symbol}")
        prices = list(prepared.prices.get(symbol, ()))
        latest = _latest(prices, "trade_date")
        if latest is None or getattr(latest, "close", None) is None:
            raise ValueError(f"security has no visible close price: {symbol}")
        candles = [
            {
                "date": row.trade_date.isoformat(),
                "open": _number(adjusted_value(prepared.adjustment_index, symbol, row.trade_date, row.open)),
                "high": _number(adjusted_value(prepared.adjustment_index, symbol, row.trade_date, row.high)),
                "low": _number(adjusted_value(prepared.adjustment_index, symbol, row.trade_date, row.low)),
                "close": _number(adjusted_value(prepared.adjustment_index, symbol, row.trade_date, row.close)),
                "volume": _number(row.vol),
            }
            for row in prices
        ]
        loaded = derived_rows or load_derived_mart_rows(
            self._session_factory,
            (symbol,),
            snapshot.as_of,
        )
        metric_rows = loaded.metrics.get(symbol, ())
        risk_rows = loaded.risks.get(symbol, ())
        content = prepared.content.get(symbol, WorkbenchContent())
        payload = {
            "symbol": symbol,
            "sidebar": prepared.sidebar,
            "quote": _quote(
                snapshot,
                security,
                latest,
                prepared.latest_basics.get(symbol),
            ),
            "dailyChart": {"candles": candles, "announcements": content.announcements,
                           **({"announcementCoverage": content.coverage} if content.coverage else {}),
                           **prepared.forecast_charts.get(symbol, {})},
            "metrics": _metric_overview(
                snapshot,
                symbol,
                metric_rows,
                list(prepared.financial_points.get(symbol, ())),
            ),
            "risks": _risk_checklist(risk_rows),
            "financial": _financial_workspace(
                snapshot,
                symbol,
                list(prepared.financial_points.get(symbol, ())),
            ),
            "isFallbackSample": False,
            "dataAvailability": {"intraday": {"available": False, "reason": "项目不要求历史分钟及实时数据"}},
        }
        payload["quote"].update(mentions=content.mentions, latestMention=content.latest_mention)
        payload['financial']['dashboard']['bands']=valuation_bands(prepared.basics.get(symbol,()))
        if symbol in prepared.return_histories:
            payload['financial']['dashboard']['returnHistory']=prepared.return_histories[symbol]
        if symbol in prepared.business_histories:
            payload['financial']['dashboard']['businessHistory']=prepared.business_histories[symbol]
        if symbol in prepared.statement_tables:
            from app.stocks.financial_runtime import patch_workspace
            payload['financial']=patch_workspace(payload['financial'],prepared.statement_tables[symbol])
        from app.stocks.operating_history import operating_history
        payload['financial']['dashboard']['operatingHistory']=operating_history(payload['financial']['statements']['tables'],snapshot.as_of)
        periods=prepared.holders.get(symbol,[])
        company=prepared.company_projections.get(symbol,{})
        payload['financial']['shareholders'].update({k:v for k,v in company.items() if k!='researchValues'})
        if periods:
            payload['financial']['shareholders'].update(periods[-1])
            payload['financial']['shareholders']['periods']=periods
        _, classification_issues = classification_fields(snapshot, security)
        return MartCandidate(f"stock-workbench:{symbol}", _as_of_datetime(snapshot.as_of), payload, MartRows(workbenches=(payload,), charts=tuple(candles)), snapshot.missing_data + classification_issues)


def project_workbench_cards(factory, workbenches):
    if factory is None:
        return tuple(workbenches)
    from app.stocks.marts.metric_cards import project_cards
    with factory() as session:
        return project_cards(session, workbenches)


class StockDailyChartBuilder:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def write(self, ts_code: str, version: str, points: Iterable[dict[str, Any]]) -> int:
        with self._session_factory.begin() as session:
            count = 0
            for point in points:
                trade_date = point.get("tradeDate") or point.get("trade_date")
                if isinstance(trade_date, str):
                    trade_date = date.fromisoformat(trade_date)
                if not isinstance(trade_date, date):
                    continue
                existing = session.scalar(select(StockDailyChart).where(StockDailyChart.ts_code == ts_code, StockDailyChart.trade_date == trade_date, StockDailyChart.snapshot_version == version))
                if existing is None:
                    session.add(StockDailyChart(ts_code=ts_code, trade_date=trade_date, snapshot_version=version, point=dict(point)))
                else:
                    existing.point = dict(point)
                count += 1
            return count
