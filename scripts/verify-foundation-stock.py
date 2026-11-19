"""在同一本机只读快照中重算指标、组合输入并独立核对分红库存。"""
import json
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import settings
from app.db.models import AdjustmentFactor, DailyPrice, PublishedSnapshot
from app.portfolio.marts.portfolio import PortfolioMartBuilder
from app.shared.readonly_database import open_readonly_factory
from app.stocks.api.financial import CoreSnapshot, years_before
from app.stocks.dataset_query import daily_rows_query, securities_query
from app.stocks.derived.inputs import load_metric_card_inputs
from app.stocks.derived.workbench_cards import calculate_metric_cards
from app.stocks.dividend_facts import dividend_observations
from app.stocks.marts.screener_list import YIELD_CARDS


def reconcile(factory, session):
    checks, mismatches = Counter(), Counter()
    def compare(name, actual, expected):
        checks[name] += 1
        if actual != expected:
            mismatches[name] += 1

    screen = session.get(PublishedSnapshot, 'screener')
    portfolio = session.get(PublishedSnapshot, 'portfolio')
    cutoff = screen.as_of.date()
    stocks = {row['symbol']:row for row in screen.payload['stocks']}
    inputs = load_metric_card_inputs(session, tuple(stocks), cutoff)
    latest = session.execute(text("""with last_price as (
        select distinct on (ts_code) ts_code,trade_date,close,id from core.daily_price
        where trade_date<=:day and ts_code=any(:symbols) order by ts_code,trade_date desc)
        select p.ts_code, p.trade_date, p.close, p.id as price_id, b.id as basic_id, b.pb, b.total_mv,
            b.data->>'pe_ttm' as pe_ttm, b.data->>'total_share' as total_share
        from last_price p left join core.daily_basic b using(ts_code, trade_date)
    """), {'day':cutoff,'symbols':list(stocks)}).mappings()
    for row in latest:
        q = inputs[row['ts_code']]['quote']
        for key, col in [('close','close'),('pb','pb'),('totalMv','total_mv'),('peTtm','pe_ttm'),('totalShare','total_share')]:
            actual, expected = q[key], row[col]
            compare('metric_input', Decimal(str(actual)) if actual is not None else None,
                    Decimal(str(expected)) if expected is not None else None)
        compare('metric_input', q['tradeDate'], row['trade_date'].isoformat())
        compare('metric_input', q['priceSource'], f"core.daily_price:{row['price_id']}")
        compare('metric_input', q['basicSource'], f"core.daily_basic:{row['basic_id']}" if row['basic_id'] else None)
    sources = Counter()
    evidence = Counter()
    undated_observations = 0
    blocked_implementations = 0
    capture_checks = 0
    for symbol, stock in stocks.items():
        cards = {c.metric_id:c.value for c in calculate_metric_cards(inputs[symbol])}
        for field, metric in YIELD_CARDS.items():
            compare('published_yield', float(cards[metric]) if cards[metric] is not None else None, stock[field])
        data, meta, issues = dividend_observations(session, symbol, date.min, date.max)
        sources.update(r['source_kind'] for r in data)
        evidence[meta['evidence_status']] += 1
        undated_observations += meta['missing_announcement_rows']
        affected = {ref for issue in issues if issue['ann_date'] is None
                    for ref in issue.get('affectedSourceIds', ())}
        blocked_implementations += len(affected)
        compare('undated_decisions', sorted(r['source_id'] for r in data
                if r['selected_implementation'] and r['source_id'] in affected), [])
        if meta['captures'] and not capture_checks:
            # 以库中首份实际报文作见证。用本机时区转换后的相邻两天探测边界。
            from datetime import datetime
            from zoneinfo import ZoneInfo
            first = min(datetime.fromisoformat(c['captured_at']) for c in meta['captures'])
            first_day = first.astimezone(ZoneInfo('Asia/Shanghai')).date()
            earlier, earlier_meta, _ = dividend_observations(session,symbol,date.min,date.max,first_day - timedelta(days=1))
            compare('capture_boundary', (len(earlier),earlier_meta['evidence_status']), (0,'missing'))
            after, seen, _ = dividend_observations(session,symbol,date.min,date.max,first_day)
            compare('capture_boundary', seen['evidence_status'], 'observed')
            for obs in after:
                compare('capture_row_day', datetime.fromisoformat(obs['captured_at']).astimezone(
                    ZoneInfo('Asia/Shanghai')).date() <= first_day, True)
            capture_checks += 1
        standard = {r['source_id']:r for r in data if r['source_kind'] == 'core'}
        expected = session.execute(text("""select id, ann_date, end_date, div_proc, cash_div_tax, cash_div,
            record_date, ex_date, pay_date from core.dividend where ts_code=:symbol and ann_date is not null
            order by id"""), {'symbol':symbol}).mappings().all()
        compare('core_dividend_keys',set(standard),{f"core.dividend:{r['id']}" for r in expected})
        for row in expected:
            version = standard.get(f"core.dividend:{row['id']}", {})
            for key, value in row.items():
                if key == 'id':
                    continue
                actual = version.get(key)
                if isinstance(value, Decimal) and actual is not None:
                    actual = Decimal(actual)
                if isinstance(value, date):
                    value = value.isoformat()
                compare('core_dividend_field', actual, value)

    payload = portfolio.payload
    starts = {s: min(p['date'] for p in values) for s, values in payload['unadjustedPrices'].items() if s != 'CNY'}
    day = portfolio.as_of.date()
    symbols = tuple(starts)
    start = years_before(day, 3)
    def daily(model):
        return tuple(session.scalars(daily_rows_query(model, start, day, symbols).order_by(model.ts_code, model.trade_date)))
    snapshot = CoreSnapshot(as_of=day, securities=tuple(session.scalars(securities_query(symbols))),
        daily_prices=daily(DailyPrice), adjustment_factors=daily(AdjustmentFactor), daily_basics=(),
        security_inputs={}, missing_data=(), financial_points=(), corporate_actions=(),
        classifications=(), macro_observations=(), content_documents=())
    shadow = PortfolioMartBuilder(factory).build(snapshot, 'audit').payload
    for key, value in shadow.items():
        if key not in ('attributionInputs','dividendYieldInputs'):
            compare('portfolio_' + key, value, payload.get(key))
    for key in ('attributionInputs','dividendYieldInputs'):
        value = shadow[key]
        by_symbol, prev = (value['assets'], payload[key]['assets']) if key=='attributionInputs' else (value, payload[key])
        compare(key + '_scope', set(by_symbol), set(prev))
        for symbol, entry in by_symbol.items():
            compare(key + '_asset', entry, prev.get(symbol))
    return dict(checked_at=session.scalar(text('select current_timestamp')).isoformat(),
        read_only=True, screener=screen.version, portfolio=portfolio.version, as_of=cutoff.isoformat(),
        symbols=len(stocks), checks=checks, mismatches=mismatches, dividend_sources=sources, evidence=evidence,
        undated_observations=undated_observations, implementations_blocked_by_undated=blocked_implementations)


if __name__ == '__main__':
    if make_url(settings.database_read_url).host not in {'localhost','127.0.0.1','::1'}:
        raise SystemExit('需要本机 PostgreSQL')
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text('show transaction_read_only')) == 'on'
        assert session.scalar(text('show transaction_isolation')) == 'repeatable read'
        result = reconcile(factory, session)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(bool(result['mismatches']))
