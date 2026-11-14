"""选股器的列表字段。金额计算使用源单位，风险信号按原站顺序发布。"""

from collections import defaultdict
from datetime import date, timedelta
from sqlalchemy import and_, select

from app.db.models import DailyPrice, Suspension, TradeCalendar
from app.shared.stock_mapping import finite_decimal
from app.stocks.derived.workbench_cards import calculate_metric_cards
from app.stocks.derived.workbench_risks import Calculation, fmt, operating

YIELD_CARDS = {
    'dividendYield': 'workbenchFiscalDividendYield',
    'expectedDividendYield': 'workbenchAnnualDividendYield',
    'expectedDividendYieldNTM': 'workbenchNtmDividendYield',
}


def valuation_windows(session, securities, sources, as_of):
    """核实窗口包含上市后十年的逐交易日记录，停牌以独立的个股记录为准。"""
    from app.stocks.valuation_calendar import _contracts, _saved

    start = date.fromisoformat(next(iter(sources.values()))['start'])
    calendars = defaultdict(dict)
    for exchange, day, opened in session.execute(select(TradeCalendar.exchange, TradeCalendar.cal_date,
            TradeCalendar.is_open).where(TradeCalendar.cal_date.between(start, as_of))):
        calendars[exchange][day] = opened
    for contract in _contracts(session):
        left, right = date.fromisoformat(contract['start']), date.fromisoformat(contract['end'])
        if right < start or right > as_of:
            continue
        days = _saved(session, contract)
        if days is None:
            continue
        open_days = set(days)
        day = max(left, start)
        while day <= right:
            existing = calendars['SSE'].get(day, day in open_days)
            calendars['SSE'][day] = existing if existing == (day in open_days) else None
            day += timedelta(days=1)
    suspended = defaultdict(set)
    suspension_days = select(Suspension.ts_code, Suspension.trade_date, DailyPrice.id, DailyPrice.vol,
                             DailyPrice.amount).outerjoin(DailyPrice, and_(DailyPrice.ts_code == Suspension.ts_code,
                             DailyPrice.trade_date == Suspension.trade_date)).where(Suspension.ts_code.in_(tuple(sources)),
                             Suspension.suspend_type == 'S', Suspension.trade_date.between(start, as_of))
    for symbol, day, price_id, volume, amount in session.execute(suspension_days):
        # 盘中停牌仍可能成交。有行情但量额均缺时，也不能证明停足全天。
        volumes = [v for v in (volume, amount) if v is not None]
        if price_id is None or (volumes and all(v == 0 for v in volumes)):
            suspended[symbol].add(day.isoformat())
    for code, source in sources.items():
        security = securities[code]
        exchange = security.exchange
        calendar = calendars[exchange]
        if exchange == 'SZSE':
            calendar = calendars['SSE'] | calendar
        rows = {r[0] for r in source['valuations']}
        reason = None
        if security.list_date is None or security.list_date > start:
            reason = f'缺上市日期或上市未满十年（上市日 {security.list_date or "未取得"}）'
        elif len(calendar) != (as_of-start).days+1 or None in calendar.values():
            reason = f'{start} 至 {as_of} 缺完整交易日历'
        else:
            expected = {day.isoformat() for day, opened in calendar.items() if opened} - suspended[code]
            gaps = sorted(expected - rows)
            if gaps:
                reason = f'缺 {len(gaps)} 个非停牌交易日估值，首日 {gaps[0]}'
            elif rows - expected:
                reason = '估值记录日期与交易日历不符，包含休市或全天停牌日'
        source['listWindowGap'] = reason


def capital_returns(reports, annual_reports, fy, values, missing):
    by_year = {int(r['year']):r for r in annual_reports}
    for width in (1, 3, 5, 10):
        key = 'capitalReturnRatio' + ('' if width == 1 else f'Cum{width}y')
        zero, undisclosed, unverified, no_dividend = [], [], [], []
        for year in range(fy-width+1, fy+1):
            report = reports.get('cashflow', {}).get(f'{year}-12-31', {}).get('values', {})
            gross = finite_decimal(report.get('c_recp_cap_contrib'))
            # 合并吸收投资现金是包含子公司在内的流入。零合计可证明组成项为零。
            if gross is None:
                undisclosed.append(str(year))
            elif gross == 0:
                zero.append(str(year))
            else:
                unverified.append(str(year))
            if by_year.get(year, {}).get('cashAmount') is None:
                no_dividend.append(str(year))
        values[key] = None
        if len(zero) == width:
            missing[key] = f'{fy-width+1} 至 {fy} 财年股权筹资现金流入为零，无融资，回报与融资之比不可算'
            continue
        gaps = []
        for years, explanation in ((undisclosed,'未披露完整股权融资现金流入'), (unverified,'仅有合并吸收投资总额，缺上市公司股权筹资分项'),
                                   (no_dividend,'未核实完整已付分红')):
            if years:
                gaps.append('、'.join(years) + ' 年' + explanation)
        gaps.append('缺完成回购金额或明确的零回购记录')
        missing[key] = '；'.join(gaps)


def financing_risk(source, annual_reports, last_period):
    if not last_period:
        return 'unknown', '缺可见合并年报及公告日'
    by_year = {int(r['year']):r for r in annual_reports}
    last = int(last_period[:4])
    levels, details = [], []
    for year in range(last-2, last+1):
        distribution = by_year.get(year, {})
        paid = finite_decimal(distribution.get('cashAmount')) if distribution.get('status') == 'complete' else None
        values = source['reports'].get('cashflow', {}).get(f'{year}-12-31', {}).get('values', {})
        flow = finite_decimal(values.get('n_cashflow_act'))
        if paid == 0:
            level, detail = 'normal', '完整已付现金分红为 0 元，无分红支出'
        elif paid is None or flow is None:
            level, detail = 'unknown', '缺完整已付现金分红金额' if paid is None else '缺年度经营活动现金流量净额'
        else:
            cover = flow / paid
            detail = f'经营现金流 {fmt(flow)} 元，实付分红 {fmt(paid)} 元，覆盖 {fmt(cover)} 倍'
            level = 'normal'
            if cover < 1:
                loans = [finite_decimal(values.get(k)) for k in ('c_recp_borrow','proc_issue_bonds','c_recp_cap_contrib')]
                if any(v is not None and v > 0 for v in loans):
                    level = 'danger'
                    detail += '，同期有融资流入'
                elif None in loans:
                    level = 'unknown'
                    detail += '，缺借款、发债或吸收投资现金流入披露'
                else:
                    level = 'warn'
                    detail += '，同期融资流入为零'
        levels.append(level)
        details.append(f'{year} 年{detail}')
    return next((level for level in ('danger','warn','unknown') if level in levels), 'normal'), '；'.join(details)


def list_risks(row, source, cash_card, annual_reports=()):
    reports = source['reports']
    balance = max(reports.get('balance', {}), default='')
    latest_fy = max((p for table in reports.values() for p in table if p.endswith('-12-31')), default='')
    results = []
    for key, rule, period in (('highCashHighDebt','high_cash_high_debt',balance),
                              ('goodwill','goodwill_risk',balance),
                              ('dividendVsFinancing','dividend_vs_financing',latest_fy)):
        card = Calculation(source, rule, period)
        if rule == 'dividend_vs_financing':
            status, detail = financing_risk(source, annual_reports, period)
            if period:
                period = f'{int(period[:4])-2}–{period[:4]} 财年'
        else:
            level = operating(card)
            detail = '；'.join(dict.fromkeys(card.missing if level == 'unknown' else card.steps))
            status = {'unknown':'unknown', 'normal':'normal', 'watch':'warn',
                      'risk':'warn' if key == 'highCashHighDebt' else 'danger'}[level]
        results.append(dict(key=key, status=status, period=period or '报告期未取得', detail=detail))
    money = row['researchValues'].get('realMoneyIndex')
    annual_points = row['financialTrends']['annual']
    period = annual_points[-1]['period'] if annual_points else '财年未取得'
    results.append(dict(key='realMoney', period=period, status='unknown' if money is None else 'warn' if money < .8 else 'normal',
                        detail=f'经营现金流 ÷ 归母净利润为 {money:.6g} 倍' if money is not None else '缺同期年报经营现金流或可作分母的归母净利润'))
    net_cash = cash_card.value if cash_card else None
    reason = '、'.join(cash_card.missing) if cash_card else '缺同报告期货币资金及核实的有息负债金额'
    results.append(dict(key='netCash', period=balance or '报告期未取得', status='unknown' if net_cash is None else 'warn' if net_cash < 0 else 'normal',
                        detail=f'货币资金减同期有息负债为 {fmt(net_cash)} 元' if net_cash is not None else reason))
    return results


def list_payouts(row, annual_reports, fy, missing):
    by_year = {int(r['year']):r for r in annual_reports}
    points = []
    issues = []
    for year in range(fy-2, fy+1):
        source = by_year.get(year, {})
        complete = source.get('status') == 'complete'
        cash, profit = finite_decimal(source.get('cashAmount')), finite_decimal(source.get('parentProfit'))
        ratio = finite_decimal(source.get('payoutRatio')) if complete else None
        if complete and cash is not None and profit is not None and profit < 0:
            ratio = cash / profit * 100
        if ratio is None or profit is None or profit <= 0:
            reason = '、'.join(source.get('missing', [])) or '缺完整已付分红或同期归母净利润'
            if complete and profit is not None and profit <= 0:
                reason = '归母净利润为零，分母为零' if profit == 0 else '归母净利润为负，亏损年份支付率不可比'
            issues.append(f'{year} 财年' + reason)
        observed = finite_decimal(source.get('observedCashAmount'))
        points.append(dict(year=str(year), complete=complete, basis=source.get('basis'),
                           querySource=source.get('querySource'), recordedThrough=source.get('recordedThrough'),
                           covered=source.get('covered', False), cashAmount=float(cash) if cash is not None else None,
                           observedCashAmount=float(observed) if observed is not None else None,
                           parentProfit=float(profit) if profit is not None else None, payoutRatio=float(ratio) if ratio is not None else None))
    row['listHistory']['dividends'] = points
    row['researchValues']['payoutRatio'] = None if issues else sum(p['payoutRatio'] for p in points)/3
    if issues:
        missing['payoutRatio'] = '；'.join(issues)


def list_projection(row, source, inputs, annual_reports, as_of):
    from app.stocks.derived.metric_dividends import PAID_CASH_BASIS
    row.setdefault('metricBasis', {}).update({key: PAID_CASH_BASIS for key in
        ('dividendYield', 'payoutRatio', 'dividendYears', 'expectedDividendYield', 'expectedDividendYieldNTM')})
    values = row['researchValues']
    missing = row.get('listMissingReasons', {})
    cards = {c.metric_id:c for c in calculate_metric_cards(inputs)} if inputs else {}
    for key, card_id in YIELD_CARDS.items():
        card = cards.get(card_id)
        values[key] = float(card.value) if card and card.value is not None else None
        if values[key] is None:
            missing[key] = '；'.join(card.missing) if card else '缺截止日可核实的分红、股价及财年输入'
    fy = as_of.year - 1
    list_payouts(row, annual_reports, fy, missing)
    from app.stocks.derived.formulas import dividend_years
    consecutive = dividend_years(inputs.get('dividends', {}) if inputs else {}, as_of.year)
    row['dividendYears'] = float(consecutive.value) if consecutive.value is not None else None
    values['dividendYears'] = row['dividendYears']
    if consecutive.value is None:
        missing['dividendYears'] = '连续分红年数缺少实际派息记录：' + '、'.join(consecutive.missing)
    for key in ('dividendYield', 'payoutRatio', 'expectedDividendYield', 'expectedDividendYieldNTM'):
        row[key] = values.get(key)
    capital_returns(source['reports'], annual_reports, fy, values, missing)
    for field, label in (('longMoneyHoldingRatio','缺同报告期国家队、社保、险资、QFII 持仓分类和股本'),
                         ('fundHeavyCount','缺同报告期完整公募重仓披露和基金去重标识')):
        if values.get(field) is None:
            missing[field] = label
    rows = source.get('valuations', [])
    for index, field in enumerate(('pe','pb'), start=1):
        key = field + 'Percentile10y'
        sample = [finite_decimal(r[index]) for r in rows]
        reason = source.get('listWindowGap')
        if not reason and ('listWindowGap' not in source or not rows or rows[-1][0] != as_of.isoformat()):
            reason = f'缺截至 {as_of} 的完整十年窗口及当日估值'
        if not reason:
            gaps = [r[0] for r, value in zip(rows, sample) if value is None]
            if gaps:
                reason = f'{field.upper()} 有 {len(gaps)} 个交易日的估值缺失或冲突，首日 {gaps[0]}'
            elif sample[-1] <= 0:
                reason = f'{as_of} 当日 {field.upper()} 不为正值'
        if reason:
            values[key] = None
            missing[key] = reason
        else:
            positives = [n for n in sample if n > 0]
            values[key] = 100 * sum(n <= sample[-1] for n in positives)/len(positives)
    row['listRisks'] = list_risks(row, source, cards.get('workbenchNetCash'), annual_reports)
    row['listMissingReasons'] = missing
