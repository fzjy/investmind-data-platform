"""工作台风险卡。所有分级比较均使用未舍入的 Decimal。"""
from decimal import Decimal
from datetime import date
from math import ceil

from app.shared.stock_mapping import finite_decimal
from app.stocks.derived.workbench_cards import DEBT_FIELDS


VERSION = "workbench-risk-v3"
RULES = {
    "high_cash_high_debt": ("存贷双高", "货币资金及有息负债分别除以总资产", "现金占比 > 25% 且有息负债占比 > 35% 时命中"),
    "goodwill_risk": ("商誉/资产", "商誉 ÷ 总资产", "> 20% 关注，> 35% 高风险"),
    "profit_structure_non_operating_dependency": ("非经营依赖度", "（投资收益＋公允价值变动收益＋营业外收入－营业外支出）÷ 归母净利润", "≥ 30% 关注，≥ 50% 高风险"),
    "impairment_pressure": ("减值压力", "资产减值和信用减值各取负值的绝对值后相加，除以归母净利润", "30%、50% 为关注和高风险阈值"),
    "comprehensive_income_gap": ("综合收益差异", "|综合收益总额－归母净利润| ÷ |归母净利润|", "≤ 10% 差异较小，> 10% 且 < 20% 仍为正常，≥ 20% 关注，≥ 40% 高风险"),
    "eps_quality_gap": ("EPS 质量差", "（基本 EPS－扣非 EPS）÷ |基本 EPS|", "15%、30% 为关注和高风险阈值"),
    "equity_cash_support": ("净资产现金流支撑", "最近最多十个年报经营现金流之和 ÷ 年末净资产", "≥ 1 倍正常，< 1 倍关注，< 0.5 倍高风险"),
    "valuation_band": ("估值分位带", "分别将 PE、PB 与十年样本的最近秩 30、50、70 分位值比较", "≤ 30 分位偏低，≥ 70 分位偏高。一项偏高为关注，两项偏高为高风险"),
    "interest_debt_burden": ("有息负债负担", "EBIT ÷ 利息支出；利息支出 ÷ 年初年末平均有息负债", "保障倍数低于 2、1 或成本达到 10%、20%，分别关注、高风险"),
    "dividend_vs_financing": ("分红与融资", "逐年比较经营现金流与该财年现金分红，并检查同期债务及股权融资流入", "覆盖不足 1 倍为关注，同年有新增融资时为高风险"),
    "dividend_sustainability": ("分红可持续性", "最近三个财年的经营现金流合计 ÷ 同期现金分红合计", "≥ 1.2 倍正常，< 1.2 倍关注，< 1 倍高风险"),
    "earnings_quality_non_recurring": ("利润结构/扣非", "逐年计算经营现金流 ÷ 扣非净利润及（归母净利润－扣非净利润）÷ 归母净利润", "现金覆盖低于 1、0.8 倍，或非经常性占比达到 30%、50%，分别关注、高风险"),
}
STATUS = {"unknown": "待评估", "normal": "正常", "watch": "关注", "risk": "高风险"}


def grade(value, warn, danger, *, low=False, strict=False):
    if value is None:
        return "unknown"
    for threshold, status in ((danger, "risk"), (warn, "watch")):
        limit = Decimal(str(threshold))
        if (value < limit if low else value > limit if strict else value >= limit):
            return status
    return "normal"


def worst(statuses):
    for status in ("risk", "unknown", "watch"):
        if status in statuses:
            return status
    return "normal"


def fmt(value):
    return format(value, ",.6f").rstrip("0").rstrip(".") if value is not None else "缺失"


class Calculation:
    def __init__(self, source, rule, period):
        self.source, self.rule, self.period = source, rule, period
        self.rows, self.steps, self.missing = [], [], []
        self.dates, self.targets = [], {}
        self.note = ""

    def read(self, table, field, period=None, *, unit="元", required=True, field_label=None):
        period = period or self.period
        report = self.source["reports"].get(table, {}).get(period, {})
        day = report.get("date", "")
        if day and period == self.period:
            self.dates.append(day)
        value = finite_decimal(report.get("values", {}).get(field))
        name = f"{period or '报告期未取得'} {field_label or f'{table}.{field}'}"
        self.rows.append(dict(label=name, value=f"{fmt(value)} {unit}" + (f"（公告 {day}）" if day else "")))
        if value is None and required:
            self.missing.append(name)
        if table in ("income", "balance", "cashflow") and value is not None:
            self.targets.setdefault(table, []).append(field)
        return value

    def ratio(self, label, numerator, denominator, *, percent=False):
        if numerator is None or denominator is None:
            self.steps.append(f"{label}：{fmt(numerator)} ÷ {fmt(denominator)}")
            return None
        if not denominator:
            self.missing.append(f"{label}的分母为零")
            self.steps.append(f"{label}：{fmt(numerator)} ÷ 0")
            return None
        value = numerator / denominator
        shown = fmt(value * 100) + "%" if percent else fmt(value) + " 倍"
        self.steps.append(f"{label}：{fmt(numerator)} ÷ {fmt(denominator)} = {shown}")
        return value

    def finish(self, status):
        title, formula, threshold = RULES[self.rule]
        if status == "unknown":
            conclusion = "、".join(dict.fromkeys(self.missing))
        else:
            evidence = self.note or (self.steps[-1] if self.steps else "")
            conclusion = "，".join(filter(None, (evidence, STATUS[status])))
        item = dict(id=self.rule, title=title, ruleVersion=VERSION, asOf=self.source["asOf"],
                    summary=conclusion, detail=formula, status=status, reportDate=self.period or "未取得",
                    announcementDate=max(self.dates, default="未取得"), reportType="年报" if self.period.endswith("12-31") else "定期报告",
                    inputs=self.rows, steps=self.steps, formula=formula, threshold=threshold,
                    conclusion=conclusion or STATUS[status], missingFields=list(dict.fromkeys(self.missing)))
        if self.targets:
            table = next(iter(self.targets))
            item["drilldown"] = dict(tab="statements", statement=table, fields=list(dict.fromkeys(self.targets[table])))
        return item


def debt(card, period):
    amounts = [card.read("balance", field, period) for field in DEBT_FIELDS]
    total = card.read("indicator", "interestdebt", period, required=False)
    if None in amounts:
        return None
    fixed, due = sum(amounts[:-1]), amounts[-1]
    if min(amounts) < 0 or (total is None and due != 0) or (total is not None and total not in (fixed, fixed + due)):
        card.missing.append(f"{period} 有息负债组成与 interestdebt 待核对")
        return None
    return fixed if total is None else total


def operating(card):
    rule = card.rule
    if rule in ("high_cash_high_debt", "goodwill_risk"):
        assets = card.read("balance", "total_assets")
        if rule == "goodwill_risk":
            ratio = card.ratio("商誉占比", card.read("balance", "goodwill"), assets, percent=True)
            return grade(ratio, ".2", ".35", strict=True)
        cash = card.ratio("现金占比", card.read("balance", "money_cap"), assets, percent=True)
        load = card.ratio("有息负债占比", debt(card, card.period), assets, percent=True)
        if None in (cash, load):
            return "unknown"
        return "risk" if cash > Decimal(".25") and load > Decimal(".35") else "normal"
    if rule == "eps_quality_gap":
        basic = card.read("income", "basic_eps", unit="元/股")
        adjusted = card.read("indicator", "verified_adjusted_eps", unit="元/股", field_label="扣非 EPS（来源待核准）")
        if adjusted is None:
            card.missing.append("扣非 EPS 缺少已核准的供给字段")
        gap = basic - adjusted if None not in (basic, adjusted) else None
        return grade(card.ratio("EPS 差异率", gap, abs(basic) if basic is not None else None, percent=True), ".15", ".30")
    profit = card.read("income", "n_income_attr_p")
    if rule == "comprehensive_income_gap":
        comprehensive = card.read("income", "t_compr_income")
        gap = comprehensive - profit if None not in (comprehensive, profit) else None
        card.steps.append(f"差额：{fmt(comprehensive)} − {fmt(profit)} = {fmt(gap)} 元")
        signed = card.ratio("差异率", gap, abs(profit) if profit is not None else None, percent=True)
        ratio = abs(signed) if signed is not None else None
        if ratio is not None:
            card.steps.append(f"按绝对幅度 {fmt(ratio * 100)}% 判定")
        if ratio is not None and ratio < Decimal(".20"):
            card.note = "差异较小" if ratio <= Decimal(".10") else "有一定差异，尚未达到关注线"
        return grade(ratio, ".20", ".40")
    if rule == "profit_structure_non_operating_dependency":
        parts = [card.read("income", field) for field in ("invest_income", "fv_value_chg_gain", "non_oper_income", "non_oper_exp")]
        total = sum(parts[:3]) - parts[3] if None not in parts else None
        card.steps.append(f"非经营项：{fmt(parts[0])} ＋ {fmt(parts[1])} ＋ {fmt(parts[2])} − {fmt(parts[3])} = {fmt(total)} 元")
    else:
        assets = card.read("income", "assets_impair_loss")
        credit = card.read("income", "credit_impa_loss", required=False)
        total = None if assets is None else -min(assets, 0) - min(credit or 0, 0)
        card.steps.append(f"损失方向的减值金额合计 {fmt(total)} 元")
        if credit is None:
            card.steps.append("信用减值损失为可选项，本期仅按资产减值损失计算")
        revenue = card.read("income", "revenue", required=False)
        if revenue:
            card.ratio("减值/收入（参考）", total, revenue, percent=True)
    ratio = card.ratio("占归母净利润比例", total, profit, percent=True)
    if profit is not None and profit < 0:
        card.missing.append("归母净利润为负，正利润分级口径不适用")
        return "unknown"
    return grade(ratio, ".30", ".50")


def valuation(card):
    rows = card.source["valuations"]
    card.missing.extend(card.source.get("valuationIssues", []))
    if not rows:
        card.missing.append("daily_basic.pe、pb 及历史交易日")
        return "unknown"
    high = 0
    if card.source.get("windowVerified") is False:
        card.missing.append("十年估值窗口的完整批量行情记录")
    for index, label in ((1, "pe"), (2, "pb")):
        sample = sorted(num for row in rows if (num := finite_decimal(row[index])) is not None and num > 0)
        current = finite_decimal(rows[-1][index])
        if not sample or current is None or current <= 0:
            card.missing.append(f"daily_basic.{label} 有效当前值或正值历史样本")
            continue
        bands = [sample[ceil(len(sample) * q / 100) - 1] for q in (30, 50, 70)]
        card.rows.append(dict(label=f"{rows[-1][0]} daily_basic.{label}", value=f"{fmt(current)} 倍"))
        card.steps.append(f"{label} 目标窗口自 {card.source['start']} 起，实际行情 {rows[0][0]} 至 {rows[-1][0]}，{len(sample)} 个正值样本，30/50/70 分位为 "
                          + " / ".join(fmt(v) for v in bands) + " 倍")
        band = "偏低" if current <= bands[0] else "偏高" if current >= bands[2] else "中性"
        card.steps.append(f"{label} {fmt(current)} 倍，{band}")
        high += band == "偏高"
    return "unknown" if card.missing else ("normal", "watch", "risk")[high]


def dividend_cash(card, year):
    from app.stocks.shareholder_returns import paid_cash_distribution
    entry = card.source["dividends"].get(str(year), {})
    distribution = paid_cash_distribution(entry, date.fromisoformat(card.source['asOf']))
    card.missing.extend(f'{year} 年：{issue}' for issue in distribution['missing'])
    for event in distribution['events']:
        if event["status"] in ("zero", "pending"):
            continue
        stamp = f"{event['period']}（公告 {event['announcedAt']}）"
        dps = finite_decimal(event["cashPerShare"])
        card.rows.append(dict(label=f"{stamp} dividend.cash_div_tax", value=f"{fmt(dps)} 元/股"))
        card.rows.append(dict(label=f'{stamp} 分配基准股数（base_share × 10000）',
                              value=f"{fmt(event['baseShares'])} 股"))
        card.rows.append(dict(label=f'{stamp} 已派现金金额', value=f"{fmt(event['cashAmount'])} 元"))
    total = distribution['cashAmount']
    if total == 0:
        card.steps.append(f"{year} 财年截至 {card.source['asOf']} 按实际派息记录累计为零")
    elif total is not None:
        card.steps.append(f'{year} 已派现金合计：逐笔税前每股现金 × 对应分配基准股数 = {fmt(total)} 元')
    return total


def annual(card):
    if not card.period:
        card.missing.append("可见合并年报及公告日")
        return "unknown"
    year = int(card.period[:4])
    period = lambda y: f"{y}-12-31"
    if card.rule == "interest_debt_burden":
        interest = card.read("income", "fin_exp_int_exp")
        cover = card.ratio("利息保障倍数", card.read("income", "ebit"), interest)
        start, end = debt(card, period(year - 1)), debt(card, card.period)
        average = (start + end) / 2 if None not in (start, end) else None
        card.steps.append(f"平均有息负债：（{fmt(start)} ＋ {fmt(end)}）÷ 2 = {fmt(average)} 元")
        cost = card.ratio("负债成本", interest, average, percent=True)
        return worst([grade(cover, 2, 1, low=True), grade(cost, ".1", ".2")])
    count = 10 if card.rule == "equity_cash_support" else 3
    first = max(2015, year - count + 1) if card.rule == "equity_cash_support" else year - count + 1
    years = range(first, year + 1)
    flows, payouts, profits, statuses = [], [], [], []
    financing_years = []
    for y in years:
        p = period(y)
        flow = card.read("cashflow", "n_cashflow_act", p)
        flows.append(flow)
        if card.rule == "equity_cash_support":
            continue
        if card.rule == "earnings_quality_non_recurring":
            adj = card.read("indicator", "verified_adjusted_profit", p, field_label="扣非净利润（来源待核准）")
            profit = card.read("income", "n_income_attr_p", p)
            conversion = card.ratio(f"{y} 现金转换", flow, adj)
            nonrec = card.ratio(f"{y} 非经常性占比", profit-adj if None not in (profit, adj) else None, profit, percent=True)
            statuses.extend([grade(conversion, 1, ".8", low=True), grade(nonrec, ".3", ".5")])
            continue
        payout = dividend_cash(card, y)
        payouts.append(payout)
        if card.rule == "dividend_vs_financing":
            if payout == 0 and flow is not None:
                card.steps.append(f'{y} 年按实际派息记录现金支出为零，无分红融资压力')
                statuses.append('normal')
                continue
            cover = card.ratio(f"{y} 现金覆盖", flow, payout)
            financing = [card.read("cashflow", f, p, required=False) for f in ("c_recp_borrow", "proc_issue_bonds", "c_recp_cap_contrib")]
            if cover is not None and cover < 1 and None in financing:
                card.steps.append(f"{y} 现金覆盖不足，部分融资流入字段缺失，按可见数据判断。")
            new_funding = any(v is not None and v > 0 for v in financing)
            status = "unknown" if cover is None else "normal" if cover >= 1 else "risk" if new_funding else "watch"
            statuses.append(status)
            if status in ("risk", "watch"):
                finding = f"{y} 年覆盖 {fmt(cover)} 倍，" + ("同期有融资流入" if new_funding else "未识别到新增融资")
                card.steps.append(f"{finding}，判为{STATUS[status]}")
                financing_years.append(finding)
        else:
            profits.append(card.read("income", "n_income_attr_p", p, required=False))
    if statuses:
        if financing_years:
            card.note = "。".join(financing_years)
        return worst(statuses)
    total = sum(flows) if flows and None not in flows else None
    if card.rule == "equity_cash_support":
        equity = card.read("balance", "total_hldr_eqy_inc_min_int")
        return grade(card.ratio("累计现金流覆盖净资产", total, equity), 1, ".5", low=True)
    payout = sum(payouts) if payouts and None not in payouts else None
    if payout == 0 and total is not None:
        card.steps.append('三年按实际派息记录现金支出合计为零，无分红现金覆盖压力')
        return 'normal'
    if payout and None not in profits:
        card.ratio("利润覆盖（参考）", sum(profits), payout)
    return grade(card.ratio("三年现金覆盖", total, payout), "1.2", 1, low=True)


def calculate_risks(source):
    from app.stocks.derived.risk_assessment import assess_current

    reports = source["reports"]
    balance = max(reports.get("balance", {}), default="")
    latest_year = max((day for rows in reports.values() for day in rows if day.endswith("12-31")), default="")
    cards = []
    for i, rule in enumerate(RULES):
        card = Calculation(source, rule, balance if i < 2 else latest_year)
        if rule == "valuation_band":
            card.period = source["valuations"][-1][0] if source["valuations"] else ""
            item = card.finish(valuation(card))
            item.update(reportType="交易日", announcementDate="按交易日取值")
        else:
            item = card.finish(operating(card) if i < 6 else annual(card))
        cards.append(item)
    return assess_current(dict(ruleVersion=VERSION, groups=[
        dict(id="operations", title="经营风险", description="资产质量、利润结构与现金流支撑", items=cards[:7]),
        dict(id="dividend", title="分红安全垫", description="估值、负债与分红可持续性", items=cards[7:]),
    ]))
