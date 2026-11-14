"""验证三表的经营趋势；累计与真实单季分开，缺失不补零。"""
from datetime import date, timedelta
from decimal import Decimal, DecimalException
from math import isfinite

FIELDS = {
    'revenue': ('income', 'revenue'), 'profit': ('income', 'n_income_attr_p'),
    'ocf': ('cashflow', 'n_cashflow_act'), 'capex': ('cashflow', 'c_pay_acq_const_fiolta'),
}
DERIVED = ('fcf', 'netMargin', 'realMoneyIndex', 'capexRate', 'revenueCagr3', 'profitCagr3')

def _date(value):
    try:
        result = date.fromisoformat(value)
        return result if result.isoformat() == value else None
    except (TypeError, ValueError):
        return None

def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        approximate = float(number)
        return number if number.is_finite() and isfinite(approximate) and (approximate != 0 or number == 0) else None
    except (DecimalException, TypeError, ValueError, OverflowError):
        return None

def _calculate(function):
    try:
        return _number(function())
    except (DecimalException, TypeError, ValueError, OverflowError):
        return None

def operating_history(tables, as_of):
    """输入已核验三表；不更改原表，不产生采集或历史可见性承诺。"""
    periods = sorted({p for t in tables for p in t.get('periods', [])
        if (d := _date(p)) is not None and d <= as_of and
        (d.month, d.day) in {(3,31),(6,30),(9,30),(12,31)}})

    def cell(field, period):
        kind, key = FIELDS[field]
        matching = [t for t in tables if t.get('kind') == kind]
        if len(matching) != 1:
            return None, '缺少唯一对应来源表', None, key
        table = matching[0]
        if table.get('amountUnit') != 'yuan':
            return None, '来源金额单位未确认，不能换算', None, key
        indices = [i for i,p in enumerate(table.get('periods', [])) if p == period]
        if len(indices) != 1:
            return None, '缺少唯一对应报告期', None, key
        metadata = [m for m in table.get('reportMetadata', []) if m.get('period') == period]
        if len(metadata) != 1:
            return None, '报告公告元数据缺失或重复', None, key
        meta = metadata[0]
        announced = _date(meta.get('announcementDate'))
        if announced is None or not _date(period) <= announced <= as_of or meta.get('reportType') != '1':
            return None, '公告日期或合并累计报告类型无效', None, key
        rows = table.get('rows', [])
        if key == 'revenue' and not any(r.get('label') == key for r in rows):
            key = 'total_revenue'
        candidates = [r for r in rows if r.get('label') == key]
        values = {_number(r.get('values', [])[indices[0]]) if len(r.get('values', [])) > indices[0] else None for r in candidates}
        value = next(iter(values)) if len(values) == 1 else None
        return value, '' if value is not None else '来源字段缺失、冲突或非有限值', announced.isoformat(), key

    cumulative = {}
    for period in periods:
        cells = {field: cell(field, period) for field in FIELDS}
        cumulative[period] = dict(period=period,
            values={k: v[0] for k,v in cells.items()},
            reasons={k: v[1] for k,v in cells.items() if v[1]},
            announcements={k:v[2] for k,v in cells.items()},
            fieldSources={k:v[3] for k,v in cells.items()})

    def derived(row, annual):
        result = dict(period=row['period'], values=dict(row['values']), reasons=dict(row['reasons']), fieldSources=dict(row['fieldSources']))
        values, reasons = result['values'], result['reasons']
        result['announcedAt'] = max((a for a in row['announcements'].values() if a), default=None)
        ocf, capex, revenue, profit = (values[k] for k in ('ocf','capex','revenue','profit'))
        values.update({k: None for k in DERIVED})
        if ocf is not None and capex is not None and capex >= 0:
            values['fcf'] = _calculate(lambda: ocf - capex)
        if profit is not None and revenue is not None and revenue > 0:
            values['netMargin'] = _calculate(lambda: profit / revenue * 100)
        if ocf is not None and profit is not None and profit > 0:
            values['realMoneyIndex'] = _calculate(lambda: ocf / profit)
        if capex is not None and capex >= 0 and revenue is not None and revenue > 0:
            values['capexRate'] = _calculate(lambda: capex / revenue * 100)
        reasons.update({key: message for key,message in (
            ('fcf', '缺经营现金流或非负资本开支，或计算无法安全表示'),
            ('netMargin', '缺归母净利润、正营业收入，或计算无法安全表示'),
            ('realMoneyIndex', '缺经营现金流、正归母净利润，或计算无法安全表示'),
            ('capexRate', '缺非负资本开支、正营业收入，或计算无法安全表示'),
        ) if values[key] is None})
        for field, name in (('revenue','revenueCagr3'),('profit','profitCagr3')):
            prior = cumulative.get(f"{int(row['period'][:4])-3:04d}-12-31") if annual else None
            start = prior['values'][field] if prior else None
            end = values[field]
            if prior and row['fieldSources'][field] == prior['fieldSources'][field] and start is not None and end is not None and start > 0 and end > 0:
                values[name] = _calculate(lambda: ((end / start) ** (Decimal(1)/3) - 1) * 100)
            if values[name] is None:
                reasons[name] = '仅年报准确三年前同口径正值端点可算CAGR3；缺失或非有限结果不计算'
        result['values'] = {k: float(v) if v is not None else None for k,v in values.items()}
        return result

    quarterly = []
    for period, row in cumulative.items():
        quarter = dict(row, values=dict(row['values']), reasons=dict(row['reasons']), announcements=dict(row['announcements']))
        month = int(period[5:7])
        if month != 3:
            prior_date = (date(int(period[:4]), month-2, 1) - timedelta(days=1)).isoformat()
            prior = cumulative.get(prior_date)
            for field in FIELDS:
                current, old = row['values'][field], prior['values'][field] if prior else None
                valid = prior and current is not None and old is not None and row['fieldSources'][field] == prior['fieldSources'][field]
                quarter['values'][field] = _calculate(lambda: current-old) if valid else None
                if quarter['values'][field] is None:
                    quarter['reasons'][field] = '本季或同年前季累计字段缺失/口径不一致，不能差分'
                elif prior:
                    quarter['announcements'][field] = max(row['announcements'][field], prior['announcements'][field])
        quarterly.append(derived(quarter, False))
    return dict(asOf=as_of.isoformat(), amountUnit='yuan', historyComplete=False,
        annual=[derived(row, True) for p,row in cumulative.items() if p.endswith('12-31')], quarterly=quarterly)
