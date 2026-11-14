"""从只读库重建真实银行样本输入，通过 HTTP 路由核对条件与模板。"""
import json
import math
import operator
from decimal import Decimal
from pathlib import Path
import sys
from typing import get_args

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.shared.snapshot_sql import PUBLISHED_SNAPSHOTS_SQL
from app.config import settings
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider
from app.stocks.derived.inputs import load_metric_card_inputs
from app.stocks.derived.metric_dividends import paid_cash_year_available
from app.stocks.models import BoardId
from app.stocks.routes import create_router


def independently_price_dividends(source):
    """沿用已核实的派息事件、财报修订选择，独立重算两个有值字段。"""
    quote = source['quote']
    cutoff = source['asOf']
    current = int(cutoff[:4])
    price = Decimal(str(quote['close']))
    shares = (Decimal(str(quote['totalShare'])) if quote['totalShare'] else Decimal(str(quote['totalMv'])) / price) * 10000
    reports = {(r['table'], r['period']): r for r in source['financials'] if r['kind'] == 'cumulative'}

    def value(table, period, field):
        row = reports.get((table, period), {})
        raw = row.get('values', {}).get(field)
        return Decimal(str(raw)) if raw is not None else None

    rates = []
    for year in (current - 2, current - 1):
        entry = source['dividends'][str(year)]
        assert paid_cash_year_available(entry, cutoff)
        dps = sum((Decimal(str(e['cashPerShare'])) for e in entry['events']
                   if e['status'] == 'paid' and e['payDate'] <= cutoff), Decimal(0))
        period = f'{year}-12-31'
        eps = value('income', period, 'basic_eps')
        if eps is None:
            eps = value('indicator', period, 'eps')
        rates.append(dps / eps)
    anchor = max(period for table, period in reports if table == 'income' and period.startswith(str(current))
                 and not period.endswith('12-31') and value('income', period, 'n_income_attr_p') is not None)
    ratios = []
    for year in range(current - 5, current):
        year_income = value('income', f'{year}-12-31', 'n_income_attr_p')
        interim = value('income', f'{year}-{anchor[5:]}', 'n_income_attr_p')
        if interim is not None and year_income is not None and 0 < interim <= year_income:
            ratios.append(interim / year_income)
    annual = value('income', anchor, 'n_income_attr_p') * len(ratios) / sum(ratios)
    payout = (3 * rates[0] + 5 * rates[1]) / 8
    return {'dividendYield': float(dps / price * 100),
            'expectedDividendYield': float(annual / shares * payout / price * 100)}, {
        'asOf': cutoff, 'close': str(price), 'shares': str(shares), 'fiscalPaidDps': str(dps),
        'seasonalPeriods': len(ratios), 'anchor': anchor, 'weightedPayout': str(payout),
        'predictedProfit': str(annual)}


def audit(session, client):
    assert session.scalar(text('show transaction_read_only')) == 'on'
    version, as_of, rows = session.execute(text(f"""
        select version, as_of, payload->'stocks' from {PUBLISHED_SNAPSHOTS_SQL} published where product='screener'
    """)).one()
    banks = {r['symbol']: r for r in rows if r['majorIndustry'] == '银行'}
    codes = sorted(banks)
    assert codes
    day = as_of.date()
    inputs = load_metric_card_inputs(session, codes, day)
    report_types = ['1','4','5','11','2','3','6','9','10','12','7','8']
    core = session.execute(text("""
        select b.ts_code, b.id as basic_id, b.pb, i.id as indicator_id, i.end_date, i.ann_date,
               i.data->>'roe' as roe from core.daily_basic b
        left join lateral (
            select * from core.financial_indicator where ts_code=b.ts_code and end_date <= :day
            and coalesce(f_ann_date,ann_date) <= :day
            order by end_date desc, array_position(cast(:report_types as text[]),report_type) nulls last,
                     coalesce(f_ann_date,ann_date) desc, update_flag desc nulls last,ann_date desc,id desc limit 1
        ) i on true where b.trade_date=:day and b.ts_code=any(:codes) order by b.ts_code
    """), {'codes': codes, 'day': day, 'report_types': report_types}).mappings()
    fields = {}
    evidence = {}
    for row in core:
        code = row['ts_code']
        yields, trace = independently_price_dividends(inputs[code])
        fields[code] = {'pb': float(row['pb']), 'roe': float(row['roe']), **yields}
        evidence[code] = {'identity': dict(row), 'values': fields[code], 'yieldInputs': trace,
                          'generalPbPercentile10y': banks[code]['researchValues'].get('pbPercentile10y'),
                          'generalPbWindowGap': banks[code]['listMissingReasons'].get('pbPercentile10y')}
    assert sorted(fields) == codes
    rules = json.loads((ROOT / 'config/financial-indicators.json').read_text(encoding='utf-8'))['bankScoring']
    categories = {key: [] for key in ('majorIndustry', 'subIndustry', 'region', 'concept')}
    body = {'filters': {'boards': list(get_args(BoardId)), 'categories': categories, 'exclusions': [], 'preset': None,
                        'thresholds': {}, 'enabledThresholds': []}, 'pageSize': 200, 'page': 1, 'sortRules': []}

    def query(bank):
        response = client.post('/api/v1/stocks/screener', json={**body, 'bank': bank})
        response.raise_for_status()
        data = response.json()['data']
        assert data['snapshot']['version'] == version
        assert len(data['items']) == data['total']
        return data

    operators = dict(gt=operator.gt, lt=operator.lt, gte=operator.ge, lte=operator.le)
    conditions = {}
    for definition in rules['fields']:
        condition = {k: definition[k] for k in ('field', 'operator', 'value')}
        key = definition['field']
        actual = query({'conditions': [condition]})
        valid = [c for c in codes if fields[c].get(key) is not None]
        expected = [c for c in valid if operators[condition['operator']](fields[c][key], condition['value'])]
        assert expected == [r['symbol'] for r in actual['items']]
        assert len(valid) == actual['bank']['availableCounts'][key]
        conditions[key] = {**condition, 'available': len(valid), 'passing': expected}

    templates = {}
    for key, preset in rules['presets'].items():
        options = {k: preset[k] for k in ('divisor', 'weights')}
        results = query({**options, 'sort': 'score-desc'})['items']
        assert [r['symbol'] for r in results] == codes
        scores = {}
        for stock in results:
            code, result = stock['symbol'], stock['bankEvaluation']
            assert result['algorithmVersion'] == rules['algorithmVersion']
            for field, actual in result['fields'].items():
                expected = fields[code].get(field)
                assert (actual is None if expected is None else math.isclose(expected, actual, abs_tol=1e-8)), (code, field, actual, expected)
            pb_score = 1 / fields[code]['pb']
            assert math.isclose(result['scores']['pb'], pb_score)
            weighted = pb_score * preset['weights']['pb'] / 100
            assert math.isclose(result['weighted']['pb'], weighted)
            missing = set(preset['weights']) - {'pb'}
            assert all(result['scores'][k] is None and result['weighted'][k] is None for k in missing)
            assert result['total'] is None and result['enabledCount'] == 5
            assert result['ahDiscount'] is None and {k for k,v in result['inputs'].items() if v is not None} == {'pb'}
            scores[code] = {'pb': pb_score, 'weightedPb': weighted, 'total': result['total']}
        templates[key] = {**options, 'scores': scores}
    return dict(snapshot=version, asOf=day, algorithm=rules['algorithmVersion'], symbols=codes,
                coreEvidence=evidence, conditions=conditions, presets=templates)


if __name__ == '__main__':
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        app = FastAPI()
        app.include_router(create_router(provider=StocksDatabaseProvider(SnapshotRepository(factory), factory)))
        with TestClient(app) as client:
            result = audit(session, client)
    print(json.dumps(result, default=str, ensure_ascii=True, indent=2))
