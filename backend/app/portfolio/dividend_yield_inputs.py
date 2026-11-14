"""组合股息率复用逐股完整查询凭据及工作台同日估算。"""
from datetime import date, timezone
from math import isfinite

from sqlalchemy import select

from app.db.models import MetricValue
from app.shared.fingerprints import canonical_source_hash
from app.stocks.api.financial import (CARD_VERSION, calculate_metric_cards, dividend_years,
                                      load_dividend_years, load_metric_card_inputs)

PAID_CASH_BASIS = 'paid-cash-records-v1'


def fiscal_summary(entry, *, year=None, history_start=None):
    events = entry['events']
    # 旧载荷保留原证据语义；新结果直接采用统一财年记录覆盖。
    complete = entry['covered'] and all(e['status'] in ('paid', 'zero') for e in events)
    dates = [r[key] for r in entry['observations'] for key in ('ann_date', 'imp_ann_date') if r[key]]
    dates.extend(e['payDate'] for e in events if e['status'] == 'paid')
    result = {
        'completeAt': max(dates) if complete and dates else None,
        'zeroConfirmed': entry['zeroConfirmed'],
        'payments': [dict(recordDate=e['recordDate'], payDate=e['payDate'], cashPerShare=float(e['cashPerShare']), sourceIds=e['sourceIds'],
                          knownAt=max([e['payDate'], e['announcedAt']] + [
                              r['imp_ann_date'] for r in entry['observations']
                              if r['sourceId'] in e['sourceIds'] and r['imp_ann_date']]))
                     for e in events if e['status'] == 'paid'],
    }
    if entry.get('basis') == PAID_CASH_BASIS:
        result.update(basis=PAID_CASH_BASIS, querySource=entry.get('querySource'),
            recordedThrough=entry['recordedThrough'], covered=entry['covered'], issues=entry['issues'])
        # completeAt 仅供旧读取端兼容，不表示全年最终不分配决议。
        result['completeAt'] = entry['recordedThrough'] if entry['covered'] else None
        if year is not None and isinstance(entry.get('queryCoverage'), dict):
            result['historyVersions'] = _fiscal_history(entry, str(year), history_start)
    return result


def _fiscal_history(entry, year, history_start):
    """只在公开版本与付款变化日重裁决，不按交易日加载数据库。"""
    cutoff = entry['recordedThrough']
    query = entry['queryCoverage']
    start = min(cutoff, max(year + '-01-01', history_start or year + '-01-01',
                           query.get('coverageStart') or year + '-01-01'))
    rows = entry['observations']
    def replay(day):
        return dividend_years(rows, date.fromisoformat(day), history_years=int(day[:4]) - int(year),
            query_coverage=query, listed_at=entry.get('listedAt'))[year]
    # 无法归属的记录可影响所有财年；单财年 observations 不得丢掉这类缺项。
    residual_issues = set(entry['issues']) - set(replay(cutoff)['issues'])
    days = {start, cutoff}
    for row in rows:
        for field in ('ann_date', 'imp_ann_date', 'pay_date'):
            value = row.get(field)
            try:
                valid = date.fromisoformat(value).isoformat() == value
            except (TypeError, ValueError):
                continue
            if valid and start <= value <= cutoff:
                days.add(value)
    versions = []
    for day in sorted(days):
        historical = replay(day)
        if residual_issues:
            historical['issues'] = sorted(set(historical['issues']) | residual_issues)
            historical.update(covered=False, zeroConfirmed=False)
        summary = fiscal_summary(historical)
        state = {key: summary[key] for key in ('covered', 'zeroConfirmed', 'issues', 'payments')}
        if versions and all(versions[-1][key] == value for key, value in state.items()):
            continue
        versions.append(dict(knownAt=day, **state))
    return versions


def load_yield_inputs(factory, starts, as_of, ledger):
    result = {symbol: {'basis': PAID_CASH_BASIS, 'recordedThrough': as_of.isoformat(),
                      'querySource': asset.get('querySource'), 'models': {}}
              for symbol, asset in ledger['assets'].items()}
    if not starts:
        return result
    query = select(MetricValue).where(MetricValue.ts_code.in_(starts),
        MetricValue.metric_id == 'workbenchNtmDividendYield', MetricValue.formula_version == CARD_VERSION,
        MetricValue.as_of.between(date.fromisoformat(min(starts.values())), as_of), MetricValue.as_of < as_of)
    with factory() as session:
        verified = [symbol for symbol in starts if result.get(symbol, {}).get('querySource')]
        history = as_of.year - int(min(starts.values())[:4]) + 3
        all_years = load_dividend_years(session, tuple(starts), as_of, history_years=history)
        for symbol, years in all_years.items():
            if symbol in verified:
                result[symbol]['fiscalYears'] = {year: fiscal_summary(entry, year=year, history_start=starts[symbol])
                                               for year, entry in years.items()}
        for row in session.scalars(query):
            day = row.as_of.isoformat()
            # 历史派生记录可能被事后修订。仅当存在当时留存的版本才供历史回看。
            stamp = row.calculated_at
            if row.as_of != as_of and (stamp is None or stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).date() > row.as_of):
                continue
            available = row.quality == 'ok' and row.unit == '%' and row.value is not None and row.value >= 0
            result[row.ts_code]['models'][day] = {
                'rate': float(row.value / 100) if available else None,
                'reason': '' if available else '；'.join(row.missing_fields or ['工作台缺少估算输入']),
                'source': f'derived.metric_value:{row.id}:{row.formula_version}',
            }
        # 当日发布必须由同版输入即时计算；第一次写入 MetricValue 不得改变第二次发布载荷。
        for symbol, inputs in load_metric_card_inputs(session, starts, as_of, dividend_years=all_years).items():
            model = next(card for card in calculate_metric_cards(inputs) if card.metric_id == 'workbenchNtmDividendYield')
            available = model.value is not None and model.value >= 0 and not model.missing
            signature = canonical_source_hash(inputs, 'stock_metric_card_inputs', symbol)
            result[symbol]['models'][as_of.isoformat()] = {
                'rate': float(model.value / 100) if available else None,
                'reason': '' if available else '；'.join(model.missing or ['工作台缺少估算输入']),
                'source': f'calculated.metric:workbenchNtmDividendYield:{CARD_VERSION}:{signature}',
            }
    return result


def valid_yield_inputs(inputs, ledger, as_of):
    assets = ledger.get('assets', {}) if isinstance(ledger, dict) else {}
    if not isinstance(inputs, dict) or inputs.keys() != assets.keys():
        return False
    for symbol, item in inputs.items():
        asset = assets[symbol]
        if not isinstance(item, dict) or not isinstance(asset, dict) or item.get('querySource') != asset.get('querySource'):
            return False
        source = item.get('querySource')
        if source is not None and (not isinstance(source, str) or not source.startswith('raw.source_payload:') or asset.get('status') != 'observed'):
            return False
        if not isinstance(item.get('models'), dict):
            return False
        if 'basis' in item and (item['basis'] != PAID_CASH_BASIS or item.get('recordedThrough') != as_of):
            return False
        if 'fiscalYears' in item and (not source or not valid_fiscal_years(item['fiscalYears'], as_of)):
            return False
        if any(row.get('basis') == PAID_CASH_BASIS and (row.get('querySource') != source
               or row.get('recordedThrough') != item.get('recordedThrough') or item.get('basis') != PAID_CASH_BASIS)
               for row in item.get('fiscalYears', {}).values()):
            return False
        for day, model in item['models'].items():
            try:
                if date.fromisoformat(day).isoformat() != day or day > as_of:
                    return False
            except (TypeError, ValueError):
                return False
            if not isinstance(model, dict) or not model.get('source') or not isinstance(model.get('reason'), str):
                return False
            rate = model.get('rate')
            if rate is None:
                if not model['reason']:
                    return False
            elif type(rate) not in (float, int) or not isfinite(rate) or rate < 0:
                return False
    return True


def valid_fiscal_years(years, as_of):
    def valid_day(value):
        try:
            return date.fromisoformat(value).isoformat() == value and value <= as_of
        except (ValueError, TypeError):
            return False

    if not isinstance(years, dict):
        return False
    for year, row in years.items():
        if not (isinstance(year, str) and len(year) == 4 and year.isdecimal() and year <= as_of[:4]
                and isinstance(row, dict) and type(row.get('zeroConfirmed')) is bool):
            return False
        modern = row.get('basis') == PAID_CASH_BASIS
        if 'basis' in row and not modern:
            return False
        if modern and (type(row.get('covered')) is not bool or not valid_day(row.get('recordedThrough'))
                or row['recordedThrough'] != as_of or not isinstance(row.get('issues'), list)
                or not all(isinstance(reason, str) for reason in row['issues'])
                or row['covered'] and (not isinstance(row.get('querySource'), str)
                    or not row['querySource'].startswith('raw.source_payload:') or row['issues'])):
            return False
        close, payments = row.get('completeAt'), row.get('payments')
        if close is not None and (not valid_day(close) or not modern and close < year + '-12-31'):
            return False
        if not isinstance(payments, list) or row['zeroConfirmed'] and (payments or not (row['covered'] if modern else close)):
            return False
        for p in payments:
            if not isinstance(p, dict) or not valid_day(p.get('payDate')) or not valid_day(p.get('knownAt')):
                return False
            cash, record = p.get('cashPerShare'), p.get('recordDate')
            if not (type(cash) in (float, int) and isfinite(cash) and cash > 0 and p['payDate'] <= p['knownAt']):
                return False
            if record is not None and (not valid_day(record) or record > p['payDate']):
                return False
            if close and close < p['knownAt']:
                return False
            if 'sourceIds' in p and (not isinstance(p['sourceIds'], list) or not p['sourceIds']
                    or not all(isinstance(source, str) and source for source in p['sourceIds'])):
                return False
        if 'historyVersions' in row:
            versions = row['historyVersions']
            if not modern or not isinstance(versions, list) or not versions:
                return False
            previous = None
            for version in versions:
                if not isinstance(version, dict) or not valid_day(version.get('knownAt')):
                    return False
                known = version['knownAt']
                if previous is not None and known <= previous:
                    return False
                previous = known
                state = {key: version.get(key) for key in ('covered', 'zeroConfirmed', 'issues', 'payments')}
                state.update(basis=PAID_CASH_BASIS, querySource=row.get('querySource'), recordedThrough=known,
                             completeAt=known if state['covered'] else None)
                if not valid_fiscal_years({year: state}, known):
                    return False
    return True
