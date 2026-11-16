"""组合分红证据只读报告；不调用供应商，不声明经济输入完整。"""
from datetime import date, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import select, text

from app.db.models import Dividend, PublishedSnapshot, SourcePayload
from app.portfolio.dividend_contract import resolve_dividend_events


DIVIDEND_FIELDS = ('ts_code', 'end_date', 'ann_date', 'div_proc', 'stk_div', 'stk_bo_rate',
    'stk_co_rate', 'cash_div', 'cash_div_tax', 'record_date', 'ex_date', 'pay_date',
    'div_listdate', 'imp_ann_date', 'base_date', 'base_share')
CAPITAL_FIELDS = ('stk_div', 'stk_bo_rate', 'stk_co_rate', 'base_date', 'base_share')


def _metadata(row):
    data = dict(row.data or {})
    # 历史通用_coerce把YYYYMMDD字符串存为整数；仅在明确日期字段上适配。
    for field in ('imp_ann_date', 'base_date'):
        value = data.get(field)
        if type(value) is int and len(str(value)) == 8:
            data[field] = str(value)
    return data


def _capital_values_complete(data):
    try:
        date.fromisoformat(str(data['base_date']))
        values = [Decimal(str(data[key])) for key in (*CAPITAL_FIELDS[:3], 'base_share')]
        return all(v.is_finite() and v >= 0 for v in values) and values[-1] > 0
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


def _capital_complete(row):
    return _capital_values_complete(_metadata(row))


def preview_portfolio_dividends(factory):
    report = {'status': 'blocked', 'requestCount': 0, 'writes': 0, 'economicsReady': False}
    engine = factory.kw['bind']
    with factory() as session:
        session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        base = session.get(PublishedSnapshot, 'screener')
        if base is None:
            return {**report, 'code': 'SCREENER_BASE_REQUIRED'}
        symbols = tuple(row.get('symbol') for row in base.payload.get('stocks', []))
        if not symbols or any(not isinstance(s, str) or not s for s in symbols) or len(symbols) != len(set(symbols)):
            return {**report, 'code': 'SCREENER_SCOPE_INVALID'}
        as_of = (base.as_of.replace(tzinfo=timezone.utc) if base.as_of.tzinfo is None else base.as_of).astimezone(timezone.utc).date()
        rows = session.scalars(select(Dividend).where(Dividend.ts_code.in_(symbols))).all()
        raw = session.scalars(select(SourcePayload).where(SourcePayload.api_name == 'dividend')).all()
        by_symbol = {symbol: set() for symbol in symbols}
        field_sets = set()
        for p in raw:
            fields, items = p.payload.get('fields'), p.payload.get('items')
            if not isinstance(fields, list) or 'ts_code' not in fields or not isinstance(items, list):
                continue
            if not all(isinstance(f, str) for f in fields) or len(set(fields)) != len(fields):
                continue
            index = fields.index('ts_code')
            for item in items:
                if not isinstance(item, list) or len(item) != len(fields):
                    continue
                symbol = item[index]
                if isinstance(symbol, str) and symbol in by_symbol:
                    by_symbol[symbol].update(fields)
                    field_sets.add(tuple(fields))
        stocks = []
        for symbol in symbols:
            own = [r for r in rows if r.ts_code == symbol]
            normalized = [{**_metadata(r), **{k: getattr(r, k) for k in ('ts_code', 'ann_date', 'end_date', 'div_proc',
                'cash_div_tax', 'record_date', 'ex_date', 'pay_date')}, 'data': _metadata(r), 'sourceId': f'core.dividend:{r.id}'} for r in own]
            contract = resolve_dividend_events(normalized, as_of)
            sources = {f'core.dividend:{r.id}': r for r in own}
            capital_complete = sum(any(source in sources and _capital_complete(sources[source])
                for source in event['sourceIds']) for event in contract['events'])
            stocks.append({'symbol': symbol, 'coreRowCount': len(own), 'cashEventCount': len(contract['events']),
                'events': contract['events'], 'issues': contract['issues'], 'historyCoverage': 'not_proven',
                'capitalFieldsCompleteEvents': capital_complete, 'capitalWindowVerified': False, 'economicsReady': False,
                'missingRawFields': [f for f in DIVIDEND_FIELDS if f not in by_symbol[symbol]]})
        from app.portfolio.dividend_capture import capture_evidence_view
        return {**report, 'status': 'review_required', 'asOf': as_of.isoformat(), 'baseVersion': base.version,
            'symbolCount': len(symbols), 'stocks': stocks, 'rawFieldSets': [list(x) for x in sorted(field_sets)],
            'capturedEvidence': capture_evidence_view(session, base, sorted(symbols), as_of),
            'requiredFields': list(DIVIDEND_FIELDS), 'missingRawFields': [f for f in DIVIDEND_FIELDS if any(f not in by_symbol[s] for s in symbols)],
            'requestPlan': {'executable': False, 'apiName': 'dividend', 'fields': list(DIVIDEND_FIELDS),
                'paramsBySymbol': [{'ts_code': symbol} for symbol in symbols], 'responseRowCap': 2000,
                'reason': '须独立冻结并预留预算后补采；不改现有股票未完成请求或据少量返回证明资本完整覆盖'},
            'blockers': ['CAPITAL_WINDOW_NOT_VERIFIED', 'HISTORY_COVERAGE_NOT_PROVEN', 'INDEPENDENT_WEALTH_NOT_PROVIDED', 'NTM_FORECAST_NOT_PROVIDED']}
