"""工作台八卡。所有比例在计算过程中为小数，入库股息率使用百分点。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, localcontext

CARD_VERSION = "v2"
DEFINITIONS = {
    "workbenchPeTtm": ("PE (TTM)", "倍", "收盘价 ÷ 连续四季基本 EPS 合计"),
    "workbenchPb": ("PB", "倍", "收盘价 ÷ 最近一期每股净资产"),
    "workbenchMarketCap": ("总市值", "元", "收盘价 × 同日总股本"),
    "workbenchNetCash": ("账上净现金", "元", "货币资金 − 同期有息负债"),
    "workbenchFiscalDividendYield": ("股息率({fy}已派·财年归属)", "%", "归属财年已派税前每股现金合计 ÷ 收盘价 × 100%"),
    "workbenchFiveYearDividendCount": ("分红次数(近5财年)", "次", "最近五个已结束财年的独立实付正现金事件数之和"),
    "workbenchAnnualDividendYield": ("预期股息率(本年度·季节性权重)", "%", "当年累计归母净利 ÷ 历史同期占比均值 ÷ 总股本 × 加权派息率 ÷ 收盘价 × 100%"),
    "workbenchNtmDividendYield": ("预期股息率(未来12月·NTM)", "%", "四季预测归母净利之和 ÷ 总股本 × 加权派息率 ÷ 收盘价 × 100%；每季预测 = 2 × 上年同季 − 前年同季"),
}
CARD_IDS = tuple(DEFINITIONS)
QUARTER_ENDS = ("03-31", "06-30", "09-30", "12-31")
DEBT_FIELDS = {"st_borr": "短期借款", "lt_borr": "长期借款", "st_bonds_payable": "短期应付债券",
               "bond_payable": "应付债券", "lease_liab": "租赁负债", "non_cur_liab_due_1y": "一年内到期的非流动负债"}


def number(value):
    try:
        parsed = Decimal(str(value))
        return parsed if parsed.is_finite() else None
    except InvalidOperation:
        return None


def quarter(period, offset=0):
    y = int(period[:4])
    y, q = divmod(y * 4 + QUARTER_ENDS.index(period[5:]) + offset, 4)
    return f"{y:04d}-{QUARTER_ENDS[q]}"


def qname(period):
    return f"{period[:4]}Q{QUARTER_ENDS.index(period[5:]) + 1}"


@dataclass
class Card:
    metric_id: str
    as_of: str
    value: Decimal | None = None
    rows: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    sources: dict = field(default_factory=dict)

    def row(self, label, value, unit=""):
        self.rows.append({"label": label, "value": "缺失" if value is None else f"{value}{unit}"})

    def require(self, condition, reason):
        if not condition and reason not in self.missing:
            self.missing.append(reason)

    def source(self, key, evidence):
        if key:
            self.sources.setdefault(key, []).append(evidence)

    def calculation(self):
        return dict(formula=DEFINITIONS[self.metric_id][2], asOf=self.as_of,
                    rows=self.rows, missing=self.missing, formulaVersion=CARD_VERSION,
                    sourceIds=sorted(self.sources))


class Reports:
    def __init__(self, rows):
        self.rows = {(r["table"], r["kind"], r["period"]): r for r in rows
                     if r["period"][5:] in QUARTER_ENDS}

    def latest(self, tables, *, year=None, interim=False):
        return max((p for table, kind, p in self.rows if table in tables
                    and (year is None or p[:4] == str(year))
                    and (not interim or p[5:] != "12-31")), default=None)

    def read(self, card, table, period, field_name, unit, kind="cumulative"):
        row = self.rows.get((table, kind, period))
        value = number(row["values"].get(field_name)) if row else None
        label = f"{qname(period)} {table}.{field_name}"
        if row:
            label += f"（公告 {row['announcedAt']}，报表 {row.get('reportType') or '指标'}，修订 {row.get('updateFlag') or '—'}）"
            for key in row.get("sourceIds", [row["sourceId"]]):
                card.source(key, [period, kind, field_name, value, row["announcedAt"], row.get("updateFlag"),
                                  field_name in row.get("conflicts", [])])
            if field_name in row.get("conflicts", []):
                label += " 同版数据冲突"
        card.row(label, value, unit)
        return value

    def conflicting(self, table, period, field_name, kind="cumulative"):
        return field_name in self.rows.get((table, kind, period), {}).get("conflicts", ())

    def profit(self, card, period):
        value = self.read(card, "income", period, "n_income_attr_p", " 元", "quarter")
        if value is not None:
            return value
        if self.conflicting("income", period, "n_income_attr_p", "quarter"):
            return None
        value = self.read(card, "income", period, "n_income_attr_p", " 元")
        if period[5:] == "03-31":
            return value
        prev = self.read(card, "income", quarter(period, -1), "n_income_attr_p", " 元")
        return value - prev if value is not None and prev is not None else None


def quote_inputs(card, quote, *, capital=False):
    day = quote.get("tradeDate") or "交易日缺失"
    price = number(quote.get("close"))
    card.row(f"{day} 未复权收盘价 daily.close", price, " 元/股")
    card.source(quote.get("priceSource"), [day, price])
    card.require(price is not None and price > 0, "缺少有效的未复权收盘价")
    shares = None
    if capital:
        raw = number(quote.get("totalShare"))
        mv = number(quote.get("totalMv"))
        card.row(f"{day} daily_basic.total_share", raw, " 万股")
        card.source(quote.get("basicSource"), [day, raw, mv])
        if raw is not None and raw > 0:
            shares = raw * 10000
        elif price is not None and price > 0 and mv is not None and mv > 0:
            card.row(f"{day} daily_basic.total_mv", mv, " 万元")
            shares = mv * 10000 / price
            card.row("反推总股本 = 总市值（万元）× 10000 ÷ 收盘价", shares, " 股")
        card.require(shares is not None, "缺少同日总股本或可反推的总市值")
        card.row("总股本", shares, " 股")
    return price, shares


def check_share_basis(card, years, basis, comparison, reason):
    """对照两个每股数值各自的基准日，只检查跨越的已实施送转。"""
    if not basis or not comparison:
        return
    start, end = sorted((basis, comparison))
    for year in years.values():
        for event in year.get("events", []):
            ratios = [event.get(key) for key in ("stockRatio", "bonusRatio", "conversionRatio")]
            if not any((number(value) or 0) > 0 for value in ratios):
                continue
            ex_date = event.get("exDate")
            if ex_date:
                if not start < ex_date <= end or ex_date > card.as_of:
                    continue
            else:
                earliest = event.get("recordDate") or event.get("announcedAt") or event["period"]
                if earliest > end or (event.get("payDate") and event["payDate"] < start):
                    continue
            for key in event.get("sourceIds", []):
                card.source(key, event)
            message = f"{reason}（基准日 {basis}、{comparison}，送转除权日 {ex_date or '缺失'}）"
            if message not in card.missing:
                card.row("送转核对（每股股数）", "，".join(
                    f"{name} {value}" for name, value in zip(("stk_div", "stk_bo_rate", "stk_co_rate"), ratios)
                    if value is not None))
                card.require(False, message)


def valuation(cards, reports, quote, years):
    pe, pb, cap, cash = cards[:4]
    for card, key, field_name in ((pe, "peTtm", "pe_ttm"), (pb, "pb", "pb")):
        observed = number(quote.get(key))
        if observed is not None:
            card.row(f"{quote['tradeDate']} 供应商独立参考值 daily_basic.{field_name}", observed, " 倍")
            card.source(quote.get("basicSource"), [quote["tradeDate"], field_name, observed])
    price, _ = quote_inputs(pe, quote)
    latest = reports.latest({"income", "indicator"})
    pe.require(latest, "缺少财报季度")
    eps_values = []
    if latest:
        for offset in range(-3, 1):
            p = quarter(latest, offset)
            eps = reports.read(pe, "income", p, "basic_eps", " 元/股", "quarter")
            pe.require(not reports.conflicting("income", p, "basic_eps", "quarter"),
                       f"{qname(p)} 单季基本 EPS 同版冲突")
            if eps is None:
                eps = reports.read(pe, "indicator", p, "q_eps", " 元/股")
            if eps is None and p[5:] == "03-31":
                eps = reports.read(pe, "income", p, "basic_eps", " 元/股")
            pe.require(eps is not None, f"缺少 {qname(p)} 单季基本 EPS")
            if eps is not None:
                eps_values.append(eps)
        check_share_basis(pe, years, quarter(latest, -4), quote.get("tradeDate"),
                          "四季基本 EPS 与收盘价的送转复权可比性待核对")
    if len(eps_values) == 4:
        ttm = sum(eps_values)
        pe.row("TTM 基本 EPS" + ("（亏损）" if ttm < 0 else ""), ttm, " 元/股")
        pe.require(ttm != 0, "TTM EPS 为零")
        if not pe.missing:
            pe.value = price / ttm
    price, _ = quote_inputs(pb, quote)
    latest = reports.latest({"indicator", "balance", "income"})
    bps = reports.read(pb, "indicator", latest, "bps", " 元/股") if latest else None
    check_share_basis(pb, years, latest, quote.get("tradeDate"), "每股净资产与收盘价的送转复权可比性待核对")
    pb.require(bps is not None, "最近一期缺每股净资产 bps")
    pb.require(bps != 0, "最近一期每股净资产为零")
    if not pb.missing:
        pb.value = price / bps
    price, shares = quote_inputs(cap, quote, capital=True)
    if not cap.missing:
        cap.value = price * shares

    period = reports.latest({"balance"})
    if not period:
        cash.require(False, "缺少已披露合并资产负债表")
        return
    row = reports.rows.get(("balance", "cumulative", period), {})
    company_type = row.get("values", {}).get("comp_type")
    cash.row("合并报表公司类型 comp_type", company_type)
    cash.require(str(company_type) not in ("2", "3", "4"), "金融行业报表缺少可比的有息负债科目")
    money = reports.read(cash, "balance", period, "money_cap", " 元")
    cash.require(money is not None, "缺少货币资金 money_cap")
    components = []
    for key, label in DEBT_FIELDS.items():
        amount = reports.read(cash, "balance", period, key, " 元")
        cash.require(amount is not None, f"缺少{label} {key}")
        cash.require(amount is None or amount >= 0, f"{label}金额为负，需核对 {key}")
        components.append(amount)
    debt = reports.read(cash, "indicator", period, "interestdebt", " 元")
    if None not in components:
        fixed = sum(components[:-1])
        due = components[-1]
        # 到期汇总可能包含无息款项，仅凭该行无法确认有息部分。
        if debt is None:
            cash.require(due == 0, "一年内到期项目含息部分缺少同期间 interestdebt 核对")
            total = fixed
        else:
            total = debt
            cash.require(debt in (fixed, fixed + due), "有息负债汇总与组成无法勾稽")
        cash.row("采用的有息负债" if not cash.missing else "待核对的有息负债", total, " 元")
        if not cash.missing:
            cash.value = money - total



def dividend_year(card, years, year, *, per_share, quote=None):
    from app.stocks.derived.metric_dividends import paid_cash_year_available
    entry = years.get(str(year), {})
    basis = entry.get("coverageBasis") or "缺少财年覆盖证据"
    card.row(f"{year} 财年核验", basis)
    for key in entry.get("sourceIds", []):
        card.source(key, [year, basis, entry.get("covered"), entry.get("issues", [])])
    resolved = {key for event in entry.get("events", []) for key in event.get("sourceIds", [])}
    shown = set()
    for obs in entry.get("observations", []):
        card.source(obs["sourceId"], obs)
        if obs.get('div_proc') not in ('实施', '实施完成', '取消', '停止', '停止实施', '终止实施', '取消实施', '不实施'):
            continue
        description = (f"{obs.get('div_proc') or '阶段缺失'}，公告 {obs.get('imp_ann_date') or obs.get('ann_date') or '缺失'}，"
                       f"派息日 {obs.get('pay_date') or '缺失'}，税前现金 {obs.get('cash_div_tax') or '未报'} 元/股")
        if obs["sourceId"] not in resolved and description not in shown:
            card.row(f"{year} 财年其他版本", description)
            shown.add(description)
    for issue in entry.get("issues", []):
        card.require(False, f"{year} 年：{issue}")
    available = paid_cash_year_available(entry, card.as_of)
    card.require(available, f"{year} 年：{basis}" if entry.get('basis') else f"{year} 年分红口径版本过旧，需重建")
    paid = []
    base_shares = set()
    for event in entry.get("events", []):
        status = event["status"]
        dps = number(event.get("cashPerShare"))
        pay = event.get("payDate")
        label = {"paid": "已派息", "missing": "待核实", "pending": "未到派息日", "zero": "零现金"}.get(status, status)
        card.row(f"{event['period']} 分配 · {label}", f"派息日 {pay or '缺失'}，税前现金 {dps if dps is not None else '缺失'} 元/股")
        for key in event.get("sourceIds", []):
            card.source(key, event)
        if status == "paid" and pay and pay <= card.as_of and dps is not None and dps > 0:
            paid.append(dps)
            if per_share:
                # 同一方案同时派现送股时，每股现金属于除权前登记的股份。
                basis = event.get("recordDate")
                if not basis and event.get("exDate"):
                    basis = (date.fromisoformat(event["exDate"]) - timedelta(days=1)).isoformat()
                target = quote.get("tradeDate") if quote else f"{year}-12-31"
                check_share_basis(card, years, basis or pay, target,
                                  f"{year} 年税前每股分红与{'股价' if quote else '年度基本 EPS'}的送转基准待核对")
            if (base := number(event.get("baseShare"))) is not None:
                base_shares.add(base)
                card.row(f"{event['period']} 分配基准股本 base_share", base, " 万股")
        elif status == "missing" or status == "paid":
            card.require(False, f"{year} 年实付分配缺少可核对的派息日或正现金金额")
    if per_share:
        if quote and base_shares and (capital := number(quote.get("totalShare"))) is not None:
            card.source(quote.get("basicSource"), [quote.get("tradeDate"), capital])
            card.row("行情日总股本 total_share", capital, " 万股")
    value = sum(paid, Decimal(0)) if per_share else len(paid)
    card.row(f"{year} 年" + ("已派合计" if available else "已记录金额") + ("税前每股现金" if per_share else "事件数"),
             value if paid or available else None, " 元/股" if per_share else " 次")
    return value


def payout(card, reports, years, fy):
    rates = []
    for year in (fy - 1, fy):
        dps = dividend_year(card, years, year, per_share=True)
        period = f"{year}-12-31"
        eps = reports.read(card, "income", period, "basic_eps", " 元/股")
        card.require(not reports.conflicting("income", period, "basic_eps"), f"{year} 年基本 EPS 同版冲突")
        if eps is None:
            eps = reports.read(card, "indicator", period, "eps", " 元/股")
        card.require(eps is not None and eps > 0, f"{year} 年缺少正的年度基本 EPS")
        entry = years.get(str(year), {})
        if eps is not None and eps > 0 and entry.get("covered") and not any(
                msg.startswith(str(year)) for msg in card.missing):
            rate = dps / eps
            rates.append(rate)
            card.row(f"{year} 年派息率 = 实付 DPS ÷ 基本 EPS", rate * 100, "%")
    if len(rates) == 2:
        weighted = (3 * rates[0] + 5 * rates[1]) / 8
        card.row("两年加权派息率 =（较早年 × 3 + 较近年 × 5）÷ 8", weighted * 100, "%")
        return weighted
    return None


def annual_profit(card, reports, year):
    anchor = None
    periods = sorted({p for table, _, p in reports.rows
                      if table == "income" and p[:4] == str(year) and p[5:] != "12-31"}, reverse=True)
    for period in periods:
        latest = reports.read(card, "income", period, "n_income_attr_p", " 元")
        if latest is not None:
            anchor = period
            break
        card.row(f"{qname(period)} 锚点筛选", "缺少可用的累计归母净利，继续核对上一报告期")
    if not anchor:
        card.require(False, f"{year} 年缺已披露中期累计归母净利")
        return None
    card.row("预测锚点", anchor)
    ratios = []
    for y in range(year - 5, year):
        p = f"{y}-{anchor[5:]}"
        interim = reports.read(card, "income", p, "n_income_attr_p", " 元")
        annual = reports.read(card, "income", f"{y}-12-31", "n_income_attr_p", " 元")
        ratio = interim / annual if interim is not None and annual not in (0, None) else None
        if interim is None or annual is None:
            reason = "排除，缺少可比利润"
        elif interim <= 0 or annual <= 0:
            reason = "排除，两端利润须为正"
        elif ratio > 1:
            reason = "排除，同期占比大于 100%"
        else:
            ratios.append(ratio)
            reason = "入选"
        card.row(f"{y} 年同期占全年比重（{reason}）", ratio * 100 if ratio is not None else None, "%")
    card.require(ratios, "前五年无合格的同期季节性样本")
    if ratios and latest is not None:
        mean = sum(ratios) / len(ratios)
        card.row("入选年度同期占比算术平均", mean * 100, "%")
        return latest / mean
    return None


def ntm_profit(card, reports):
    anchor = reports.latest({"income"})
    if not anchor:
        card.require(False, "缺少最新已披露利润表季度")
        return None
    card.row("实际报告季锚点", anchor)
    forecasts = []
    for step in range(1, 5):
        target = quarter(anchor, step)
        previous = quarter(target, -4)
        earlier = quarter(target, -8)
        values = []
        for period in (previous, earlier):
            v = reports.profit(card, period)
            card.row(f"{qname(period)} 实际单季归母净利", v, " 元")
            card.require(v is not None, f"预测 {qname(target)} 缺少 {qname(period)} 单季归母净利")
            values.append(v)
        prediction = None
        if None not in values:
            prediction = 2 * values[0] - values[1]
            forecasts.append(prediction)
        card.row(f"{qname(target)} 预测 = 2 × {qname(previous)} − {qname(earlier)}", prediction, " 元")
    return sum(forecasts) if len(forecasts) == 4 else None


def calculate_metric_cards(inputs):
    with localcontext() as ctx:
        ctx.prec = 50
        as_of = inputs["asOf"]
        quote = inputs["quote"]
        years = inputs["dividends"]
        reports = Reports(inputs["financials"])
        fy = int(as_of[:4]) - 1
        cards = [Card(key, as_of) for key in CARD_IDS]
        valuation(cards, reports, quote, years)
        fiscal, count, annual, ntm = cards[4:]
        price, _ = quote_inputs(fiscal, quote)
        dps = dividend_year(fiscal, years, fy, per_share=True, quote=quote)
        if not fiscal.missing:
            fiscal.value = dps / price * 100
        events = sum(dividend_year(count, years, y, per_share=False) for y in range(fy - 4, fy + 1))
        if not count.missing:
            count.value = Decimal(events)
        for card, profit in ((annual, annual_profit(annual, reports, fy + 1)), (ntm, ntm_profit(ntm, reports))):
            price, shares = quote_inputs(card, quote, capital=True)
            rate = payout(card, reports, years, fy)
            card.row("预测归母净利合计", profit, " 元")
            card.require(profit is None or profit >= 0, "预测亏损，盈利不足以估算股息率")
            eps = profit / shares if profit is not None and shares else None
            forecast_dps = eps * rate if eps is not None and eps >= 0 and rate is not None else None
            card.row("预测 EPS", eps, " 元/股")
            card.row("预测每股现金分红", forecast_dps, " 元/股")
            if not card.missing:
                card.value = forecast_dps / price * 100
        for card in cards:
            if card.value is not None:
                stored = card.value.quantize(Decimal("0.0000000001")) if abs(card.value) < 10**18 else None
                card.require(stored is not None and abs(stored) < 10**18, "计算结果超出数据库数值范围")
                card.value = stored if not card.missing else None
            card.row("计算结果", card.value, " " + DEFINITIONS[card.metric_id][1])
        return cards


def metric_value_card(row):
    return display_card(row.metric_id, row.as_of.year, row.value, row.calculation)


def display_card(metric_id, year, amount, calculation):
    label, unit, _ = DEFINITIONS[metric_id]
    label = label.format(fy=year - 1)
    value = "--"
    if amount is not None:
        if unit == "元":
            value = f"{amount / 10**8:.2f} 亿元"
        elif unit == "次":
            value = f"{amount:.0f} 次"
        else:
            value = f"{amount:.2f}" + ("%" if unit == "%" else "")
    return dict(id=metric_id, label=label, value=value, calculation=calculation)
