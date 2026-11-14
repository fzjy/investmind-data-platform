"""字段诊断不得改变旧卡算式或把不同公司的完整字段拼成共同样本。"""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date

import pytest

from app.macro.enterprise import (
    EnterprisePolicy, MarketValueInput, ReceivableInput, TradingDayInput,
    _hash,
)
from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis
from backend.tests.macro.test_enterprise_rea import controlled

PERIOD = date(2024, 6, 30)
AS_OF = date(2024, 12, 1)
FIELDS = {
    'scissors': ['income.n_income_attr_p', 'income.revenue'],
    'inventory': ['balance.inventories', 'income.revenue'],
    'white-strip': ['balance.accounts_receiv', 'income.revenue'],
    'lying-flat': ['cashflow.c_pay_acq_const_fiolta', 'cashflow.n_cashflow_act'],
    'dividend': ['cashflow.c_pay_acq_const_fiolta', 'cashflow.c_pay_dist_dpcp_int_exp'],
    'loss': ['daily_basic.total_mv', 'income.n_income_attr_p'],
}


def inputs(count=6):
    data = controlled(count)
    return replace(data,
        financials=tuple(replace(r, values={**r.values, **(
            {'n_income_attr_p': r.values['n_income']} if r.table == 'income' else
            {'inventories': 10, 'accounts_receiv': 10} if r.table == 'balance' else {})})
            for r in data.financials),
        cashflow_enabled=True, distribution_enabled=True, loss_enabled=True,
        trading_days=(TradingDayInput(date(2024, 9, 1), False, 'cal:1'),
                      TradingDayInput(date(2024, 9, 2), True, 'cal:2')),
        market_values=tuple(MarketValueInput(s, date(2024, 9, 2), 100, 'mv:' + s) for s in data.symbols))


def build(data=None, as_of=AS_OF):
    return public_enterprise_analysis(data or inputs(), as_of, EnterprisePolicy())


def quality(result, identifier, period=PERIOD):
    return next(q for c in result['cards'] if c['id'] == identifier
                for q in c['quality'] if q['period'] == str(period))


def mutate(data, symbol, table, period, field, value):
    return replace(data, financials=tuple(replace(r, values={**r.values, field: value})
        if (r.symbol, r.table, r.end_date) == (symbol, table, period) else r for r in data.financials))


def test_all_six_cards_dynamic_61_members_and_exact_field_coverage():
    result = build(inputs(61))
    assert valid_enterprise_analysis(result)
    for identifier, fields in FIELDS.items():
        q = quality(result, identifier)
        assert q['sampleCount'] == q['candidateCount'] == 61
        assert q.get('fieldCoverage') == dict.fromkeys(fields, 61), identifier


def test_distinct_field_gaps_do_not_create_a_fake_common_sample():
    data = inputs()
    data = mutate(data, data.symbols[0], 'income', PERIOD, 'revenue', None)
    data = mutate(data, data.symbols[1], 'income', PERIOD, 'n_income_attr_p', None)
    q = quality(build(data), 'scissors')
    assert q['sampleCount'] == 4 and q['candidateCount'] == 6
    assert q.get('fieldCoverage') == {'income.n_income_attr_p': 5, 'income.revenue': 5}


@pytest.mark.parametrize('when', [date(2024, 3, 31), date(2023, 6, 30), date(2023, 3, 31)])
def test_growth_fields_require_every_current_and_year_ago_quarter_input(when):
    data = inputs()
    result = build(mutate(data, data.symbols[0], 'income', when, 'revenue', None))
    assert quality(result, 'scissors').get('fieldCoverage') == {
        'income.n_income_attr_p': 6, 'income.revenue': 5}


def test_legacy_cash_and_dividend_require_their_own_periods_not_rea_periods():
    data = inputs()
    data = mutate(data, data.symbols[0], 'cashflow', date(2023, 6, 30), 'n_cashflow_act', None)
    data = mutate(data, data.symbols[0], 'cashflow', date(2023, 6, 30), 'c_pay_acq_const_fiolta', None)
    result = build(data)
    assert quality(result, 'lying-flat').get('fieldCoverage') == dict.fromkeys(FIELDS['lying-flat'], 6)
    assert quality(result, 'dividend').get('fieldCoverage') == dict.fromkeys(FIELDS['dividend'], 6)
    data = mutate(data, data.symbols[0], 'cashflow', date(2023, 3, 31), 'c_pay_dist_dpcp_int_exp', None)
    assert quality(build(data), 'dividend').get('fieldCoverage') == {
        'cashflow.c_pay_acq_const_fiolta': 6, 'cashflow.c_pay_dist_dpcp_int_exp': 5}


@pytest.mark.parametrize('payment', [None, -1, 5])
def test_missing_negative_or_decreasing_payment_does_not_poison_valid_net_cash(payment):
    data = inputs()
    data = mutate(data, data.symbols[0], 'cashflow', PERIOD, 'c_pay_acq_const_fiolta', payment)
    data = mutate(data, data.symbols[1], 'cashflow', PERIOD, 'n_cashflow_act', -100)
    assert quality(build(data), 'lying-flat').get('fieldCoverage') == {
        'cashflow.c_pay_acq_const_fiolta': 5, 'cashflow.n_cashflow_act': 6}


def test_legal_zero_and_negative_profit_remain_valid_field_inputs():
    data = inputs()
    data = mutate(data, data.symbols[0], 'income', PERIOD, 'n_income_attr_p', -1)
    data = mutate(data, data.symbols[1], 'balance', PERIOD, 'inventories', 0)
    result = build(data)
    assert quality(result, 'scissors').get('fieldCoverage') == dict.fromkeys(FIELDS['scissors'], 6)
    assert quality(result, 'inventory').get('fieldCoverage') == dict.fromkeys(FIELDS['inventory'], 6)


@pytest.mark.parametrize('mode', ['matched', 'changed', 'future', 'null-core'])
def test_receivable_field_coverage_uses_bound_visible_supplement_without_overwriting_null(mode):
    data = inputs()
    row = next(r for r in data.financials if (r.symbol, r.table, r.end_date) ==
               (data.symbols[0], 'balance', PERIOD))
    values = dict(row.values)
    if mode == 'null-core': values['accounts_receiv'] = None
    else: del values['accounts_receiv']
    row = replace(row, values=values)
    data = replace(data, financials=tuple(row if r.source_key == row.source_key else r for r in data.financials),
        receivables=(ReceivableInput(row.source_key, _hash(asdict(row)) if mode != 'changed' else 'f'*64,
            'matched', '12', 'a'*64, date(2025, 1, 1) if mode == 'future' else date(2024, 7, 10)),))
    assert quality(build(data), 'white-strip').get('fieldCoverage') == {
        'balance.accounts_receiv': 6 if mode == 'matched' else 5, 'income.revenue': 6}


def test_classification_history_and_listing_constraints_apply_to_each_field():
    data = inputs()
    data = replace(data, industries=tuple(replace(r, effective_from=date(2024, 1, 1))
        if r.symbol == data.symbols[0] else r for r in data.industries))
    q = quality(build(data), 'inventory')
    assert q.get('fieldCoverage') == dict.fromkeys(FIELDS['inventory'], 5)
    assert q['candidateCount'] == 6


def test_loss_profit_and_market_value_are_independent_but_observation_date_is_required():
    data = inputs()
    data = mutate(data, data.symbols[0], 'income', PERIOD, 'n_income_attr_p', None)
    data = replace(data, market_values=tuple(r for r in data.market_values if r.symbol != data.symbols[1]))
    q = quality(build(data), 'loss')
    assert q['sampleCount'] == 4 and q['candidateCount'] == 6
    assert q.get('fieldCoverage') == {'income.n_income_attr_p': 5, 'daily_basic.total_mv': 5}
    q = quality(build(data, as_of=date(2024, 8, 1)), 'loss')
    assert q['valuationStatus'] == 'valuation-window-not-due'
    assert q.get('fieldCoverage') == dict.fromkeys(FIELDS['loss'], 0)


def test_loss_contract_does_not_accept_claimed_coverage_without_an_observation_day():
    result = build(as_of=date(2024, 8, 1))
    q = quality(result, 'loss')
    q['fieldCoverage'] = {'income.n_income_attr_p': 6, 'daily_basic.total_mv': 6}
    assert not valid_enterprise_analysis(result)


@pytest.mark.parametrize('damage', ['boolean', 'negative', 'excess', 'below-sample', 'missing', 'extra', 'null'])
def test_public_contract_rejects_malformed_optional_field_coverage(damage):
    result = build()
    q = quality(result, 'scissors')
    q['fieldCoverage'] = dict.fromkeys(FIELDS['scissors'], 6)
    if damage == 'missing': del q['fieldCoverage']['income.revenue']
    elif damage == 'extra': q['fieldCoverage']['income.unknown'] = 6
    elif damage == 'null': q['fieldCoverage'] = None
    else: q['fieldCoverage']['income.revenue'] = {'boolean': True, 'negative': -1, 'excess': 7, 'below-sample': 5}[damage]
    assert not valid_enterprise_analysis(result), damage


def test_old_contract_is_accepted_without_fabricating_field_coverage():
    result = build()
    for c in result['cards']:
        for q in c['quality']: q.pop('fieldCoverage', None)
    assert valid_enterprise_analysis(result)


@pytest.mark.parametrize('nested', [False, True])
def test_field_counts_must_allow_the_claimed_common_sample_intersection(nested):
    from backend.tests.macro.test_enterprise_industries import inputs as industry_inputs
    data = inputs()
    data = mutate(data, data.symbols[0], 'income', PERIOD, 'revenue', None)
    data = mutate(data, data.symbols[1], 'income', PERIOD, 'n_income_attr_p', None)
    if nested:
        classification = industry_inputs(6)
        data = replace(data, industries=classification.industries, industry_labels=classification.industry_labels)
    result = public_enterprise_analysis(data, AS_OF, EnterprisePolicy(), industry_enabled=nested)
    assert valid_enterprise_analysis(result)
    target = result['industryComparison']['groups'][0]['analysis'] if nested else result
    q = quality(target, 'scissors')
    assert q['sampleCount'] < q['candidateCount']
    q['fieldCoverage'] = dict.fromkeys(FIELDS['scissors'], q['candidateCount'])
    assert not valid_enterprise_analysis(result), 'complete fields cannot have an incomplete intersection'


def test_additive_diagnostics_preserve_entire_old_public_payload():
    result = deepcopy(build())
    for c in result['cards']:
        for q in c['quality']: q.pop('fieldCoverage', None)
    assert _hash(result) == '9f7072798d609dd381ce35238c679a9b2a6d0b683195b9d21da66a558d02a21f'
