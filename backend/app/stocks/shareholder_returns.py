"""股东回报专用投影；金额以元、股份以股、比例以百分数发布。"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from sqlalchemy import select
from app.db.models import DailyBasic
from app.stocks.derived.metric_dividends import load_dividend_years, paid_cash_year_available


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def paid_basis_conflict(event, observations):
    """仅比较同一现金事件派息前最后实施版本与派息后明确变更的基数。"""
    pay = event.get('payDate')
    if not pay or event.get('status') != 'paid':
        return False
    matches = []
    for row in observations:
        if row.get('div_proc') not in ('实施', '实施完成') or row.get('end_date') != event.get('period'):
            continue
        if row.get('record_date') is not None and event.get('recordDate') is not None:
            related = row['record_date'] == event['recordDate']
        else:
            related = bool(row.get('ex_date') and row['ex_date'] == event.get('exDate'))
        if not related:
            continue
        if row.get('pay_date') != pay or decimal(row.get('cash_div_tax')) != decimal(event.get('cashPerShare')):
            continue
        announced = max(row.get('imp_ann_date') or '', row.get('ann_date') or '')
        if announced:
            matches.append((announced, row))
    prior = [item for item in matches if item[0] <= pay]
    if not prior:
        return False
    last = max(day for day, _ in prior)
    baselines = [row for day, row in prior if day == last]
    for day, row in matches:
        if day <= pay:
            continue
        for old in baselines:
            # 缺字段不构成更正；不拿窄合同空值覆盖已经核验过的基准。
            old_shares, new_shares = decimal(old.get('base_share')), decimal(row.get('base_share'))
            if old_shares is not None and new_shares is not None and old_shares != new_shares:
                return True
            if old.get('base_date') and row.get('base_date') and old['base_date'] != row['base_date']:
                return True
    return False


def paid_cash_distribution(entry, as_of):
    """按每笔分配基数计算实付金额；不依赖利润、价格或年末股本。"""
    events, paid = [], []
    missing = list(entry.get('issues', []))
    for event in entry.get('events', []):
        dps = decimal(event.get('cashPerShare'))
        shares = decimal(event.get('baseShare'))
        basis = event.get('baseDate')
        eligibility = event.get('recordDate') or event.get('exDate')
        shares = shares * 10000 if shares is not None and shares > 0 and basis and eligibility and basis <= eligibility and bool(event.get('sourceIds')) else None
        cash = dps * shares if dps is not None and shares is not None else Decimal(0) if dps == 0 else None
        if paid_basis_conflict(event, entry.get('observations', [])):
            shares = None
            cash = None
            missing.append('事件 ' + ','.join(event.get('sourceIds', [])) + ' 派息后分配基数更正冲突')
        detail = event | dict(baseShares=shares, cashAmount=cash)
        events.append(detail)
        if event['status'] in ('paid', 'zero'):
            paid.append(detail)
            if cash is None:
                missing.append('事件 ' + ','.join(event.get('sourceIds', [])) + ' 缺可核验分配股数或金额')
    complete = paid_cash_year_available(entry, as_of)
    if not complete:
        missing.append(entry.get('coverageBasis') or '实际派息查询覆盖不足或口径版本需重建')
    dps = sum((decimal(e['cashPerShare']) for e in paid), Decimal(0)) if paid else Decimal(0) if complete else None
    known_cash = [e['cashAmount'] for e in paid if e['cashAmount'] is not None]
    observed_cash = sum(known_cash, Decimal(0)) if known_cash or complete else None
    cash = observed_cash if len(known_cash) == len(paid) else None
    return dict(covered=complete, events=events, missing=missing, observedDps=dps,
                observedCashAmount=observed_cash, dps=dps if complete else None,
                cashAmount=cash if complete else None)


def annual_distributions(years, profits, as_of):
    rows = []
    for year in sorted(set(years) | set(profits)):
        entry = years.get(year, {})
        amounts = paid_cash_distribution(entry, as_of)
        complete = amounts['covered']
        events = [event | dict(fiscalYear=year) for event in amounts['events']]
        missing = amounts['missing']
        cash = amounts['cashAmount']
        profit = decimal(profits.get(year))
        if profit is None or profit <= 0:
            missing.append('年度归母净利润缺失或非正')
        ratio = cash / profit * 100 if complete and not entry.get('issues') and cash is not None and profit is not None and profit > 0 else None
        rows.append(dict(year=year, asOf=as_of.isoformat(), basis=entry.get('basis'),
                         querySource=entry.get('querySource'), recordedThrough=entry.get('recordedThrough'),
                         covered=complete, sourceIds=entry.get('sourceIds', []),
                         dps=amounts['dps'],
                         cashAmount=cash, observedDps=amounts['observedDps'],
                         observedCashAmount=amounts['observedCashAmount'], parentProfit=profit, payoutRatio=ratio,
                         status='complete' if complete and cash is not None else 'partial' if events else 'missing',
                         events=events, missing=sorted(set(missing))))
    return rows


def concentration_periods(counts, basics, as_of):
    grouped = defaultdict(list)
    for row in counts:
        if row.ann_date and row.ann_date <= as_of and row.end_date <= as_of:
            grouped[row.end_date].append(row)
    result = []
    for period, items in sorted(grouped.items()):
        latest = max(r.ann_date for r in items)
        values = {decimal(r.holder_num) for r in items if r.ann_date == latest}
        count = next(iter(values)) if len(values) == 1 else None
        caps = {decimal(r.total_mv) for r in basics if r.trade_date == period}
        cap = next(iter(caps)) if len(caps) == 1 else None
        cap = cap * 10000 if cap is not None and cap > 0 else None
        valid = count is not None and count > 0 and count == count.to_integral_value()
        missing = ([] if valid else ['股东户数缺失、冲突或非正整数']) + ([] if cap is not None else ['缺统计日同日总市值'])
        result.append(dict(period=period.isoformat(), holderCount=count, marketCap=cap,
                           averageAmount=cap / count if valid and cap is not None else None, missing=missing))
    return result


def load_shareholder_returns(session, symbols, as_of, *, card_inputs=None):
    """只读同一事务。返回 symbol -> annualDistributions/concentrationPeriods。"""
    from app.stocks.derived.inputs import load_metric_card_inputs
    codes = tuple(symbols)
    inputs = card_inputs
    if inputs is None:
        years = load_dividend_years(session, codes, as_of, history_years=None)
        inputs = load_metric_card_inputs(session, codes, as_of, dividend_years=years)
    from app.stocks.shareholder_counts import load_shareholder_counts
    counts = load_shareholder_counts(session, codes, as_of)
    periods = {r.end_date for r in counts if r.end_date <= as_of}
    basics = session.scalars(select(DailyBasic).where(DailyBasic.ts_code.in_(codes), DailyBasic.trade_date.in_(periods))).all()
    counts_by_code, basics_by_code = defaultdict(list), defaultdict(list)
    for row in counts:
        counts_by_code[row.ts_code].append(row)
    for row in basics:
        basics_by_code[row.ts_code].append(row)
    result = {}
    for code in codes:
        profits = {r['period'][:4]: r['values'].get('n_income_attr_p') for r in inputs[code]['financials']
                   if r['table'] == 'income' and r['period'].endswith('-12-31') and r['kind'] == 'cumulative'
                   and 'n_income_attr_p' not in r['conflicts']}
        result[code] = dict(annualDistributions=annual_distributions(inputs[code]['dividends'], profits, as_of),
                           concentrationPeriods=concentration_periods(counts_by_code[code],
                               basics_by_code[code], as_of))
    return result

