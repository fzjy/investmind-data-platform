from datetime import date
import json

from app.db.models import Security, DailyBasic, DailyPrice, FinancialIndicator, IncomeStatement
from app.stocks.derived.inputs import load_metric_card_inputs
from app.stocks.derived.metric_dividends import dividend_years
from app.portfolio.evidence import verified_dividend_rows

from backend.tests.test_dividend_ledger import saved, row, SYMBOL


def query(raw_id=10):
    return dict(querySource=f'raw.source_payload:{raw_id}', sourceIds=[f'raw.source_payload:{raw_id}'],
                coverageStart='1990-01-01', coverageEnd='2026-11-14', issues=[])


def test_paired_market_rows_visible_consolidated_revisions_and_symbol_isolation(pg_factory):
    with pg_factory.begin() as s:
        s.add_all(Security(ts_code=code, symbol=code[:6], name="样本") for code in (SYMBOL, "600002.SH"))
        s.flush()
        d = date.fromisoformat
        day = d('2026-10-31')
        s.add_all([DailyPrice(ts_code=SYMBOL, trade_date=day, close=20),
            DailyBasic(ts_code=SYMBOL, trade_date=d('2026-10-30'), total_mv=50, data={'total_share': 12}),
            FinancialIndicator(ts_code=SYMBOL, end_date=d('2026-09-30'), ann_date=day, data={'bps': 3})])
        for code, report, ann, value in (
            (SYMBOL, '1', '2026-10-01', 1), (SYMBOL, '4', '2026-10-21', 2),
            (SYMBOL, '6', '2026-10-23', 3), (SYMBOL, '1', '2026-11-01', 4),
            (SYMBOL, '2', '2026-10-02', 5), ('600002.SH', '1', '2026-10-01', 6)):
            s.add(IncomeStatement(ts_code=code, end_date=d('2026-09-30'), report_type=report,
                ann_date=d(ann), data={'n_income_attr_p': value}))
        inputs = load_metric_card_inputs(s, [SYMBOL], day)
        json.dumps(inputs, allow_nan=False)
        data = inputs[SYMBOL]
        assert data['quote']['totalShare'] is None and data['quote']['basicSource'] is None
        assert data['quote']['tradeDate'] == day.isoformat()
        assert [(v['kind'], v['values']['n_income_attr_p']) for v in data['financials']
                if v['table'] == 'income'] == [('cumulative', 2), ('quarter', 5)]
        assert data['financials'][-1]['values']['bps'] == 3


def test_verified_original_reused_across_settlement_and_zero_requires_query_coverage(saved):
    s, _, _, _, raw, put = saved
    put([row()])
    raw_id, rows = verified_dividend_rows(s, [SYMBOL])[SYMBOL]
    assert raw_id == raw.id
    for cutoff, status in [('2026-05-11', 'pending'), ('2026-05-12', 'paid')]:
        year = dividend_years(rows, date.fromisoformat(cutoff), raw_id, query_coverage=query(raw_id))['2025']
        assert year['events'][0]['status'] == status and year['covered']
        assert year['events'][0]['payDate'] == '2026-05-12'
        assert year['events'][0]['sourceIds'] == [rows[0]['sourceId']]
    put([row(cash_div_tax=0, pay_date=None, record_date=None, ex_date=None)])
    raw_id, rows = verified_dividend_rows(s, [SYMBOL])[SYMBOL]
    assert dividend_years(rows, date(2026, 9, 1), raw_id, query_coverage=query(raw_id))['2025']['zeroConfirmed']
    assert not dividend_years(rows, date(2026, 9, 1))['2025']['zeroConfirmed']


def test_missing_pay_date_or_amendment_after_payment_blocks_annual_sum():
    target = date(2026, 8, 1)
    original = row(sourceId='raw.source_payload:10:row:1')
    assert dividend_years([original | {'pay_date': None}], target, 10)['2025']['issues']
    for change in ({'div_proc': '取消'}, {'cash_div_tax': '0.8'}):
        revised = original | change | {'ann_date': '20260601', 'imp_ann_date': '20260601', 'sourceId': 'core.dividend:7'}
        year = dividend_years([original, revised], target, 10)['2025']
        assert not year['covered'] and year['issues'] == ['已派息记录与后续更正或撤回冲突']
        assert len(year['sourceIds']) == 2
    before_pay = revised | {'div_proc': '取消', 'ann_date': '20260510', 'imp_ann_date': '20260510'}
    removed = dividend_years([original, before_pay], target, 10)['2025']
    assert removed['events'] == [] and removed['issues'] == [] and not removed['zeroConfirmed']


def test_previous_full_original_cancellation_survives_current_manifest_omission(saved):
    from copy import deepcopy
    from app.db.models import JobCursor, IngestionBatch, SourcePayload
    from app.ingestion.repositories import content_hash
    from app.portfolio.evidence import JOB
    s, _, control, unit, raw, put = saved
    put([row(), row(div_proc='取消', ann_date='20260601', imp_ann_date='20260601')])
    historic = {k: v for k, v in control.cursor_value.items() if k != 'identity'} | {'cycleDate': '2026-11-01'}
    key = content_hash(historic)
    s.add(JobCursor(job_name=JOB, cursor_key='capture:' + key, cursor_value=historic | {'identity': key}))
    batch = IngestionBatch(id='historical-batch', dataset='portfolio_dividend_evidence', source='tushare',
        status='completed', request_count=1, row_count=2, rejected_count=0)
    saved_raw = SourcePayload(batch_id=batch.id, api_name='dividend', request_hash=raw.request_hash,
        content_hash=raw.content_hash, payload=deepcopy(raw.payload))
    s.add_all([batch, saved_raw])
    s.flush()
    s.add(JobCursor(job_name=JOB, cursor_key=f'{key}:unit:{SYMBOL}',
        cursor_value=unit.cursor_value | {'batchId': batch.id, 'rawId': saved_raw.id}))
    put([row()])
    primary, rows = verified_dividend_rows(s, [SYMBOL])[SYMBOL]
    annual = dividend_years(rows, date(2026, 8, 1), primary)['2025']
    assert annual['issues'] == ['已派息记录与后续更正或撤回冲突'] and not annual['covered']


def test_narrow_core_observation_deduplicated_only_if_values_match():
    original = row(sourceId='raw.source_payload:10:row:1')
    narrow = original | dict(sourceId='core.dividend:2', stk_div=None, imp_ann_date=None)
    day = date(2026, 8, 1)
    year = dividend_years([narrow, original], day, 10, query_coverage=query())['2025']
    assert len(year['events']) == 1 and year['covered']
    conflict = dividend_years([narrow | dict(cash_div_tax='0.6'), original], day, 10)['2025']
    assert not conflict['covered'] and '同一版本的分配原始记录相互冲突' in conflict['issues']


def test_transient_cancellation_remains_visible_after_omission_and_duplicate_responses(pg_factory):
    from app.connectors.tushare.contracts import TushareResponse
    from app.connectors.tushare.strategies import Dataset
    from app.stocks.update.planning import plan_for
    from app.stocks.update.repository import StockUpdateRepository
    factory = pg_factory
    day = date(2026, 8, 1)
    with factory.begin() as session:
        session.add(Security(ts_code=SYMBOL, symbol=SYMBOL[:6], name="样本"))
    plan = plan_for(Dataset.DIVIDEND, {'ts_code': SYMBOL})
    repo = StockUpdateRepository(factory)
    original = row()
    cancelled = row(div_proc='取消', ann_date='20260601', imp_ann_date='20260601')
    def observe(items):
        response = TushareResponse(fields=plan.fields, items=tuple(tuple(r.get(f) for f in plan.fields) for r in items))
        assert repo.ingest_unit('test', plan, response, day, date(2020, 1, 1))['status'] == 'completed'
        with factory() as session:
            return load_metric_card_inputs(session, [SYMBOL], day)[SYMBOL]['dividends']['2025']
    observed = observe([original, cancelled])
    assert '已派息记录与后续更正或撤回冲突' in observed['issues']
    assert observe([original]) == observed
    assert observe([cancelled, original]) == observed
    with factory() as session:
        prior = load_metric_card_inputs(session, [SYMBOL], date(2026, 5, 20))[SYMBOL]['dividends']['2025']
        assert '已派息记录与后续更正或撤回冲突' not in prior['issues'] and prior['events'][0]['status'] == 'paid'
