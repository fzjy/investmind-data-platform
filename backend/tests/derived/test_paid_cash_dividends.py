"""实际派现金额按归属财年累计，完整查询与零值保持独立证据。"""
from datetime import date
from decimal import Decimal

import pytest

from app.stocks.derived.metric_dividends import dividend_years
from app.stocks.derived.workbench_cards import calculate_metric_cards
from app.stocks.shareholder_returns import annual_distributions
from backend.tests.derived.test_workbench_metric_cards import fixture
from backend.tests.test_dividend_ledger import row


def coverage(**changes):
    return dict(querySource='raw.source_payload:10', sourceIds=['raw.source_payload:10'],
                coverageStart='1991-04-03', coverageEnd='2026-11-14', issues=[]) | changes


def years(rows=(), day=date(2026, 11, 14), **kwargs):
    return dividend_years(rows, day, 10, query_coverage=coverage(), listed_at=date(1991, 4, 3), **kwargs)


def pingan():
    return [row(end_date='20250630', ann_date='20250901', imp_ann_date='20250901',
                record_date='20251010', ex_date='20251013', pay_date='20251013', div_listdate=None,
                cash_div_tax='.236', stk_div=None, sourceId='raw.source_payload:10:row:1'),
            row(ann_date='20260601', imp_ann_date='20260601', record_date='20260611',
                ex_date='20260612', pay_date='20260612', div_listdate=None, cash_div_tax='.360',
                stk_div=None, sourceId='raw.source_payload:10:row:2')]


def test_no_query_never_promotes_empty_observations_to_zero():
    entry = dividend_years([], date(2026, 11, 14), 10)['2025']
    assert entry.get('basis') == 'paid-cash-records-v1'
    assert not entry['covered'] and not entry['zeroConfirmed']


def test_successful_empty_history_is_recorded_zero_including_current_year():
    for year in ('2024', '2025', '2026'):
        entry = years()[year]
        assert entry['covered'] and entry['zeroConfirmed']
        assert entry['querySource'] == 'raw.source_payload:10'
        assert entry['recordedThrough'] == '2026-11-14'
        assert entry['sourceIds'] == ['raw.source_payload:10']
        assert '最终' not in entry['coverageBasis']


@pytest.mark.parametrize('day,amount,count', [
    (date(2026, 11, 14), '.596', 2), (date(2026, 6, 11), '.236', 1),
    (date(2025, 10, 12), '0', 0),
])
def test_paid_cash_is_fiscal_owned_and_payment_cutoff_filtered(day, amount, count):
    entry = years(pingan(), day)['2025']
    paid = [e for e in entry['events'] if e['status'] == 'paid']
    assert entry['covered']
    assert sum((Decimal(e['cashPerShare']) for e in paid), Decimal(0)) == Decimal(amount)
    assert len(paid) == count
    assert entry['zeroConfirmed'] == (count == 0)


def test_duplicate_implementation_versions_and_unimplemented_zero_proposal():
    first = pingan()[0] | dict(cash_div_tax='.40')
    revised = first | dict(ann_date='20250902', imp_ann_date='20250902', sourceId='raw.source_payload:10:row:2')
    proposal = row(div_proc='预案', cash_div_tax='0', pay_date=None, sourceId='raw.source_payload:10:row:3')
    entry = years([first, revised, proposal])['2025']
    assert entry['covered'] and not entry['issues']
    assert [e['cashPerShare'] for e in entry['events']] == ['0.4']


@pytest.mark.parametrize('query', [None, coverage(issues=['truncated']),
    coverage(coverageEnd='2026-11-13'), coverage(coverageStart=None), coverage(querySource=None),
    coverage(coverageStart='2025-07-01')])
def test_missing_or_expired_coverage_never_creates_zero(query):
    entry = dividend_years([], date(2026, 11, 14), 10, query_coverage=query)['2025']
    assert not entry['covered'] and not entry['zeroConfirmed']


def test_listing_year_is_valid_but_prelisting_years_are_unavailable():
    result = dividend_years([], date(2026, 11, 14), 10,
        query_coverage=coverage(coverageStart='2024-07-01'), listed_at=date(2024, 7, 1))
    assert not result['2023']['covered'] and not result['2023']['zeroConfirmed']
    assert result['2024']['covered'] and result['2024']['zeroConfirmed']


def test_cash_fields_and_conflicts_block_only_relevant_fiscal_year():
    event = pingan()[0]
    for change in ({'pay_date': None}, {'cash_div_tax': None}, {'cash_div_tax': '-1'}):
        result = years([event | change])
        assert not result['2025']['covered'] and result['2025']['issues']
        assert result['2024']['covered']
    revised = event | dict(cash_div_tax='.5', sourceId='raw.source_payload:10:row:2')
    assert not years([event, revised])['2025']['covered']


def test_zero_implementation_needs_neither_identity_nor_payment_date():
    event = pingan()[0] | dict(cash_div_tax=0, record_date=None, ex_date=None, pay_date=None)
    assert years([event])['2025']['zeroConfirmed']


def test_old_year_contract_must_be_rebuilt_before_cards_infer_zero():
    inputs = fixture()
    inputs['dividends']['2025'] = dict(covered=True, zeroConfirmed=True, events=[], issues=[])
    assert calculate_metric_cards(inputs)[4].value is None


def test_recorded_zero_cards_keep_price_profit_and_history_gaps_independent():
    inputs = fixture()
    inputs['dividends'] = years(day=date.fromisoformat(inputs['asOf']))
    fiscal, count, annual, ntm = calculate_metric_cards(inputs)[4:]
    assert [c.value for c in (fiscal, count, annual, ntm)] == [0, 0, 0, 0]
    inputs['quote']['close'] = None
    fiscal, count, *_ = calculate_metric_cards(inputs)[4:]
    assert fiscal.value is None and count.value == 0
    inputs['quote']['close'] = '10'
    inputs['financials'] = []
    fiscal, count, annual, ntm = calculate_metric_cards(inputs)[4:]
    assert fiscal.value == count.value == 0 and annual.value is ntm.value is None


def test_shareholder_pending_payment_does_not_hide_recorded_amount_or_zero():
    result = years(pingan(), date(2026, 6, 11))
    entry = annual_distributions({'2025': result['2025']}, {'2025': 100}, date(2026, 6, 11))[0]
    assert entry['dps'] == Decimal('.236')
    zero = annual_distributions({'2024': years()['2024']}, {'2024': 100}, date(2026, 11, 14))[0]
    assert zero['cashAmount'] == zero['payoutRatio'] == 0
    assert zero['basis'] == 'paid-cash-records-v1'


def test_consecutive_years_are_anchored_to_latest_fiscal_and_stop_on_recorded_zero():
    from app.stocks.derived.formulas import dividend_years as consecutive
    records = years(pingan())
    assert consecutive(records, 2026).value == 1
    assert consecutive(years(), 2026).value == 0
    records['2025']['covered'] = False
    assert consecutive(records, 2026).value is None


def test_core_formula_inputs_use_shared_zero_instead_of_legacy_cash(pg_factory, monkeypatch):
    from app.db.models import Security
    from app.stocks.derived.inputs import CoreSnapshotRepository

    factory = pg_factory
    with factory.begin() as session:
        session.add(Security(ts_code='600001.SH', symbol='600001', name='test', status='L'))
    monkeypatch.setattr('app.stocks.derived.metric_dividends.load_dividend_years',
        lambda *args, **kwargs: {'600001.SH': years()})
    values = CoreSnapshotRepository(factory).load(date(2026, 11, 14), ('600001.SH',)).security_inputs['600001.SH'].values
    assert values['cash_dividend'] == values['latest_dps'] == 0
    assert values['dividend_history'] == (0, 0, 0)


def test_risk_uses_paid_only_and_zero_needs_no_share_denominator():
    from app.stocks.derived.workbench_risks import Calculation, dividend_cash
    source = dict(asOf='2026-06-11', dividends=years(day=date(2026, 6, 11)), reports={})
    card = Calculation(source, 'dividend_sustainability', '2025-12-31')
    assert dividend_cash(card, 2025) == 0
    assert not card.missing and '累计为零' in card.steps[0]


def test_company_annual_returns_use_shared_recorded_zero_and_ignore_old_audit():
    from app.stocks import shareholder_supply
    records = annual_distributions(years(), {str(y): 100 for y in range(2021, 2027)}, date(2026, 11, 14))
    result = shareholder_supply.paid_annual_returns(records, date(2026, 11, 14), price=10)
    latest = next(r for r in result['annualReturns'] if r['year'] == '2025')
    assert latest['basis'] == 'paid-cash-records-v1' and latest['complete']
    assert latest['dps'] == latest['payoutRatio'] == 0
    assert result['researchValues']['dividendYield'] == result['researchValues']['payoutRatio'] == 0


def test_invalid_fiscal_date_never_becomes_query_backed_zero():
    result = years([pingan()[0] | dict(end_date='20251331')])
    assert not result['2025']['covered'] and result['2025']['issues']


def test_failed_alias_evidence_is_isolated_per_security(pg_factory, monkeypatch):
    from app.stocks.derived import metric_dividends

    def originals(session, symbols):
        if '920047.BJ' in symbols:
            raise ValueError('alias_query_failed')
        return {symbol: (10, []) for symbol in symbols}
    monkeypatch.setattr(metric_dividends, 'verified_dividend_rows', originals)
    monkeypatch.setattr('app.portfolio.api.dividends.dividend_query_coverage',
        lambda session, symbols: {s: coverage() for s in symbols})
    with pg_factory() as session:
        result = metric_dividends.load_dividend_years(session, ['920047.BJ', '000001.SZ'], date(2026, 11, 14))
    assert not result['920047.BJ']['2025']['covered']
    assert result['000001.SZ']['2025']['zeroConfirmed']


def test_cash_card_details_exclude_proposals_but_keep_implementation_evidence():
    inputs = fixture()
    proposals = [row(div_proc=stage, cash_div_tax=None, sourceId=f'raw.source_payload:10:row:{i}')
                 for i, stage in enumerate(('预案', '股东大会通过'), 3)]
    inputs['dividends'] = years([*pingan(), *proposals], date.fromisoformat(inputs['asOf']))
    details = calculate_metric_cards(inputs)[4].rows
    assert not any('预案' in r['value'] or '股东大会通过' in r['value'] for r in details)
    assert any('已派息' in r['label'] for r in details)


def test_query_gap_detail_translates_machine_reasons_and_retains_codes():
    entry = dividend_years([], date(2026, 11, 14), query_coverage=coverage(
        issues=['query_missing:430047.BJ', 'mapping_query_missing']))['2025']
    assert '430047.BJ' in entry['coverageBasis'] and '尚未采集' in entry['coverageBasis']
    assert '代码变更对照' in entry['coverageBasis']
    assert all('query_missing' not in issue for issue in entry['issues'])


def test_zero_dividend_risk_keeps_no_outflow_normal_without_zero_denominator_error():
    from app.stocks.derived.workbench_risks import calculate_risks
    source = dict(asOf='2026-11-14', dividends=years(), valuations=[], reports={})
    for y in range(2023, 2026):
        for table, values in (('income', {'n_income_attr_p':100}), ('cashflow', {'n_cashflow_act':100})):
            source['reports'].setdefault(table, {})[f'{y}-12-31'] = dict(date=f'{y+1}-04-01', values=values)
    result = calculate_risks(source)
    relevant = [r for group in result['groups'] for r in group['items']
                if r['id'] in ('dividend_vs_financing', 'dividend_sustainability')]
    assert all(r['status'] == 'normal' and not r['missingFields'] for r in relevant)


def test_list_inputs_keep_more_than_five_years_for_consecutive_history(pg_factory, monkeypatch):
    from app.db.models import Security
    from app.stocks.derived.inputs import load_metric_card_inputs
    from app.stocks.derived import metric_dividends
    from app.stocks.derived.formulas import dividend_years as consecutive

    rows = [row(end_date=f'{y}1231', ann_date=f'{y+1}0501', imp_ann_date=f'{y+1}0501',
                record_date=f'{y+1}0510', ex_date=f'{y+1}0511', pay_date=f'{y+1}0512',
                div_listdate=None, stk_div=None, sourceId=f'raw.source_payload:10:row:{y}') for y in range(2020, 2026)]
    monkeypatch.setattr(metric_dividends, 'verified_dividend_rows', lambda *args: {'600001.SH': (10, rows)})
    monkeypatch.setattr('app.portfolio.api.dividends.dividend_query_coverage',
        lambda *args: {'600001.SH': coverage(coverageStart='2020-01-01')})
    with pg_factory.begin() as session:
        session.add(Security(ts_code='600001.SH', symbol='600001', name='test', status='L', list_date=date(2020, 1, 1)))
        session.flush()
        inputs = load_metric_card_inputs(session, ['600001.SH'], date(2026, 11, 14))['600001.SH']
    assert consecutive(inputs['dividends'], 2026).value == 6


def test_cancelled_proposal_without_implementation_does_not_block_paid_zero():
    cancelled = row(div_proc='停止实施', record_date=None, ex_date=None, pay_date=None,
                    cash_div_tax=None, sourceId='raw.source_payload:10:row:1')
    result = years([cancelled])['2025']
    assert result['covered'] and result['zeroConfirmed'] and not result['issues']


def test_history_recalculation_contract_preserves_query_and_full_observations():
    from app.stocks.api import financial
    original = pingan()[0] | dict(div_listdate='20251014', _verifiedOriginal=True,
                                  _originalTsCode='430047.BJ')
    correction = original | dict(cash_div_tax='.5', sourceId='raw.source_payload:10:row:2')
    entry = years([original, correction])['2025']
    assert not entry['covered'] and entry['queryCovered']
    assert entry['queryCoverage'] == coverage()
    assert entry['observations'][0]['ts_code'] == original['ts_code']
    assert entry['observations'][0]['div_listdate'] == '2025-10-14'
    assert entry['observations'][0]['_verifiedOriginal']
    assert entry['observations'][0]['_originalTsCode'] == '430047.BJ'
    assert financial.dividend_years is dividend_years
    from app.stocks.derived.inputs import load_metric_card_inputs
    assert financial.load_metric_card_inputs is load_metric_card_inputs
    assert financial.calculate_metric_cards is calculate_metric_cards


@pytest.mark.parametrize('base_share,expected', [('20000', Decimal('80000000')), (None, None)])
def test_risk_cash_reuses_paid_event_base_instead_of_year_end_capital(base_share, expected):
    from app.stocks.derived.workbench_risks import Calculation, dividend_cash
    event = pingan()[0] | dict(cash_div_tax='.4', base_share=base_share, base_date='20250901')
    entry = years([event])['2025']
    source = dict(asOf='2026-11-14', dividends={'2025':entry}, reports={
        'balance': {'2025-12-31': dict(date='2026-04-01', values={'total_share':'100000000'})}})
    shared = annual_distributions({'2025':entry}, {}, date(2026, 11, 14))[0]
    card = Calculation(source, 'dividend_sustainability', '2025-12-31')
    assert dividend_cash(card, 2025) == shared['cashAmount'] == expected
    assert bool(card.missing) == (expected is None)
    assert not any('balance.total_share' in row['label'] for row in card.rows)


def test_risk_cash_preserves_paid_basis_revision_conflict():
    from app.stocks.derived.workbench_risks import Calculation, dividend_cash
    event = pingan()[0] | dict(cash_div_tax='.4', base_share='20000', base_date='20250901')
    entry = years([event])['2025']
    entry['observations'].append(entry['observations'][0] | dict(
        ann_date='2025-09-02', imp_ann_date='2025-09-02', base_share='10000'))
    entry['observations'].append(entry['observations'][0] | dict(
        ann_date='2025-10-14', imp_ann_date='2025-10-14', base_share='20000'))
    card = Calculation(dict(asOf='2026-11-14', dividends={'2025':entry}, reports={}),
                       'dividend_sustainability', '2025-12-31')
    assert dividend_cash(card, 2025) is None
    assert any('分配基数更正冲突' in reason for reason in card.missing)
