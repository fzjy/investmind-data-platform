"""组合财年投影按实付记录计算，兼容旧载荷但不提升旧证据。"""
from copy import deepcopy
from datetime import date, datetime, timezone
import json
from time import perf_counter

import pytest

from app.portfolio.dividend_yield_inputs import fiscal_summary, valid_fiscal_years, valid_yield_inputs


BASIS = 'paid-cash-records-v1'
SOURCE = 'raw.source_payload:7'
DAY = '2026-06-11'


def entry(**updates):
    return dict(basis=BASIS, querySource=SOURCE, recordedThrough=DAY, covered=True,
                zeroConfirmed=True, events=[], observations=[], issues=[]) | updates


def test_successful_empty_query_projects_calculable_zero_without_final_decision():
    row = fiscal_summary(entry())
    assert row['basis'] == BASIS
    assert row['querySource'] == SOURCE
    assert row['recordedThrough'] == DAY
    assert row['covered'] is True and row['zeroConfirmed'] is True
    assert row['payments'] == [] and row['issues'] == []
    assert valid_fiscal_years({'2025': row, '2026': row}, DAY)


def test_pending_payment_does_not_block_recorded_cash_and_missing_fields_keep_reason():
    paid = dict(status='paid', payDate='2025-10-15', announcedAt='2025-09-10',
                recordDate='2025-10-14', cashPerShare='0.236', sourceIds=['s'])
    row = fiscal_summary(entry(zeroConfirmed=False, events=[paid, dict(status='pending')]))
    assert row['covered'] is True
    assert [p['cashPerShare'] for p in row['payments']] == [.236]
    partial = fiscal_summary(entry(covered=False, zeroConfirmed=False, events=[paid], issues=['缺少派息日']))
    assert partial['covered'] is False and partial['issues'] == ['缺少派息日']
    assert len(partial['payments']) == 1


@pytest.mark.parametrize('change', ['source', 'future', 'unknown-basis', 'zero-with-payment', 'missing-covered'])
def test_new_projection_rejects_unbound_or_inconsistent_record_coverage(change):
    row = fiscal_summary(entry())
    if change == 'source': row['querySource'] = None
    if change == 'future': row['recordedThrough'] = '2026-06-12'
    if change == 'unknown-basis': row['basis'] = 'future-version'
    if change == 'zero-with-payment': row['payments'] = [dict(recordDate=None, payDate=DAY, knownAt=DAY, cashPerShare=.1)]
    if change == 'missing-covered': row.pop('covered')
    assert not valid_fiscal_years({'2025': row}, DAY)


def test_legacy_projection_remains_legacy_and_payload_sources_cannot_disagree():
    legacy = fiscal_summary(dict(covered=True, zeroConfirmed=False, events=[], observations=[]))
    assert legacy.get('basis') != BASIS
    assert legacy['completeAt'] is None
    assert valid_fiscal_years({'2025': legacy}, DAY)
    ledger = {'assets': {'A': {'querySource': SOURCE, 'status': 'observed'}}}
    inputs = {'A': dict(basis=BASIS, querySource=SOURCE, recordedThrough=DAY, models={},
                        fiscalYears={'2025': fiscal_summary(entry())})}
    assert valid_yield_inputs(inputs, ledger, DAY)
    bad = deepcopy(inputs)
    bad['A']['fiscalYears']['2025']['querySource'] = 'raw.source_payload:8'
    assert not valid_yield_inputs(bad, ledger, DAY)


def test_history_replays_initial_paid_version_before_same_cash_revision():
    from app.stocks.derived.metric_dividends import dividend_years
    from backend.tests.test_dividend_ledger import row
    first = row(end_date='20241231', ann_date='20250415', imp_ann_date='20250415',
                record_date='20250519', ex_date='20250520', pay_date='20250520', div_listdate=None,
                cash_div_tax='.4', stk_div='0', sourceId='raw.source_payload:7:row:1')
    revised = first | dict(ann_date='20250601', imp_ann_date='20250601', base_share='200',
                          sourceId='raw.source_payload:7:row:2')
    query = dict(querySource=SOURCE, sourceIds=[SOURCE], coverageStart='1991-01-01',
                 coverageEnd='2025-06-30', issues=[])
    original = dividend_years([first, revised], date(2025, 6, 30), 7, query_coverage=query)['2024']
    result = fiscal_summary(original, year='2024')
    assert result['payments'][0]['knownAt'] == '2025-06-01'
    versions = result['historyVersions']
    early = [v for v in versions if v['knownAt'] <= '2025-05-30'][-1]
    assert early['covered'] and early['payments'][0]['cashPerShare'] == .4
    assert early['payments'][0]['knownAt'] == '2025-05-20'
    assert early['payments'][0]['sourceIds'] == ['raw.source_payload:7:row:1']
    assert valid_fiscal_years({'2024': result}, '2025-06-30')


def test_history_keeps_early_payment_but_late_paid_conflict_remains_missing():
    from app.stocks.derived.metric_dividends import dividend_years
    from backend.tests.test_dividend_ledger import row
    first = row(end_date='20241231', ann_date='20250415', imp_ann_date='20250415',
                record_date='20250519', ex_date='20250520', pay_date='20250520', div_listdate=None,
                cash_div_tax='.4', stk_div='0', sourceId='raw.source_payload:7:row:1')
    late = first | dict(ann_date='20250601', imp_ann_date='20250601', cash_div_tax='.5', sourceId='raw.source_payload:7:row:2')
    query = dict(querySource=SOURCE, sourceIds=[SOURCE], coverageStart='1991-01-01', coverageEnd='2025-06-30', issues=[])
    original = dividend_years([first, late], date(2025, 6, 30), 7, query_coverage=query)['2024']
    result = fiscal_summary(original, year='2024')
    early = [v for v in result['historyVersions'] if v['knownAt'] <= '2025-05-30'][-1]
    assert early['covered'] and early['payments'][0]['cashPerShare'] == .4
    assert result['covered'] is False and result['issues']
    assert result['historyVersions'][-1]['covered'] is False


@pytest.mark.parametrize('query_issue', [None, 'query_failed:600001.SH'])
def test_unassigned_late_record_only_blocks_history_after_it_became_public(query_issue):
    from app.stocks.derived.metric_dividends import dividend_years
    from backend.tests.test_dividend_ledger import row
    records = [row(end_date=None, ann_date='20261101', imp_ann_date='20261101',
                   sourceId=SOURCE + ':row:1')]
    query = dict(querySource=SOURCE, sourceIds=[SOURCE], coverageStart='1991-01-01',
                 coverageEnd='2026-12-31', issues=[query_issue] if query_issue else [])
    original = dividend_years(records, date(2026, 12, 31), query_coverage=query)['2025']
    before = dividend_years(records, date.fromisoformat(DAY), query_coverage=query)['2025']
    result = fiscal_summary(original, year='2025')
    historical = [v for v in result['historyVersions'] if v['knownAt'] <= DAY][-1]
    assert historical['covered'] == before['covered']
    assert historical['zeroConfirmed'] == before['zeroConfirmed']
    assert historical['issues'] == before['issues']
    assert not result['covered'] and result['issues']
    late = [v for v in result['historyVersions'] if v['knownAt'] <= '2026-11-01'][-1]
    assert not late['covered'] and any('归属财年' in issue for issue in late['issues'])
    assert valid_fiscal_years({'2025': result}, '2026-12-31')


@pytest.mark.parametrize('change', ['empty', 'future-version', 'unordered', 'future-payment', 'false-zero'])
def test_historical_projection_validation_rejects_unsafe_versions(change):
    row = fiscal_summary(entry())
    state = dict(knownAt='2026-05-20', covered=True, zeroConfirmed=True, issues=[], payments=[])
    row['historyVersions'] = [state]
    if change == 'empty': row['historyVersions'] = []
    if change == 'future-version': state['knownAt'] = '2026-06-12'
    if change == 'unordered': row['historyVersions'] = [state, state.copy()]
    if change == 'future-payment':
        state.update(zeroConfirmed=False, payments=[dict(recordDate=None, payDate=DAY, knownAt=DAY, cashPerShare=.4)])
    if change == 'false-zero': state['covered'] = False
    assert not valid_fiscal_years({'2025': row}, DAY)


def test_fifty_security_history_stays_sparse_and_validates_without_daily_rows():
    from app.stocks.derived.metric_dividends import dividend_years
    from backend.tests.test_dividend_ledger import row
    started = perf_counter()
    cutoff, window_start = '2026-06-30', '2021-01-01'
    inputs, assets = {}, {}
    for index in range(50):
        symbol = f'{index:06d}.SZ'
        source = f'raw.source_payload:{index + 1}'
        records = []
        for fiscal_year in range(2018, 2026):
            paid_year = fiscal_year + 1
            first = row(ts_code=symbol, end_date=f'{fiscal_year}1231', ann_date=f'{paid_year}0415', imp_ann_date=f'{paid_year}0415',
                record_date=f'{paid_year}0519', ex_date=f'{paid_year}0520', pay_date=f'{paid_year}0520', div_listdate=None,
                cash_div_tax='.4', stk_div='0', sourceId=f'{source}:row:{2*fiscal_year}')
            records.extend([first, first | dict(ann_date=f'{paid_year}0601', imp_ann_date=f'{paid_year}0601', sourceId=f'{source}:row:{2*fiscal_year+1}')])
        query = dict(querySource=source, sourceIds=[source], coverageStart='1991-01-01', coverageEnd=cutoff, issues=[])
        years = dividend_years(records, date.fromisoformat(cutoff), query_coverage=query, history_years=8)
        inputs[symbol] = dict(basis=BASIS, querySource=source, recordedThrough=cutoff, models={}, fiscalYears={
            year: fiscal_summary(value, year=year, history_start=window_start) for year, value in years.items()})
        assets[symbol] = dict(status='observed', querySource=source)
    build_seconds = perf_counter() - started
    all_versions = [value['historyVersions'] for item in inputs.values() for value in item['fiscalYears'].values()]
    assert max(map(len, all_versions)) <= 3
    assert all(v['knownAt'] >= window_start for versions in all_versions for v in versions)
    started = perf_counter()
    assert valid_yield_inputs(inputs, dict(assets=assets), cutoff)
    validation_seconds = perf_counter() - started
    print(f'Synthetic 50 securities: fiscal rows={len(all_versions)}, sparse versions={sum(map(len, all_versions))}, '
          f'JSON bytes={len(json.dumps(inputs, ensure_ascii=False).encode())}, '
          f'build_seconds={build_seconds:.4f}, validate_seconds={validation_seconds:.4f}')


def test_historical_model_requires_a_version_retained_on_its_own_day(pg_factory):
    from app.db.models import MetricValue
    from app.portfolio.dividend_yield_inputs import load_yield_inputs

    factory = pg_factory
    with factory.begin() as session:
        for day, saved in [('2026-05-20', '2026-05-20'), ('2026-05-21', '2026-06-01')]:
            session.add(MetricValue(ts_code='000001.SZ', metric_id='workbenchNtmDividendYield',
                as_of=date.fromisoformat(day), value=4, unit='%', formula_version='v2', quality='ok', missing_fields=[],
                calculated_at=datetime.combine(date.fromisoformat(saved), datetime.min.time(), timezone.utc)))
    ledger = dict(assets={'000001.SZ': dict(status='missing', querySource=None)})
    model = load_yield_inputs(factory, {'000001.SZ': '2026-05-01'}, date.fromisoformat(DAY), ledger)['000001.SZ']['models']
    assert model['2026-05-20']['rate'] == .04
    assert '2026-05-21' not in model
    assert model[DAY]['rate'] is None and model[DAY]['reason']
    assert model[DAY]['source'].startswith('calculated.metric:workbenchNtmDividendYield:v2:')
