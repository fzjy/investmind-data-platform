"""白条必须同组聚合、严格绑定专用证据，不能覆盖既有字段。"""
from dataclasses import asdict, replace
from datetime import date, timedelta

import pytest

from app.macro import enterprise as m
from backend.tests.macro.test_enterprise import inputs


def enabled(data=None):
    assert hasattr(m, 'ReceivableInput'), 'white-strip supplement input is missing'
    data = data or inputs()
    evidence = []
    for row in data.financials:
        if row.table != 'balance':
            continue
        value = {('000001.SZ', 2023): '100', ('000001.SZ', 2024): '150',
                 ('000002.SZ', 2023): '900', ('000002.SZ', 2024): '990'}[(row.symbol, row.end_date.year)]
        evidence.append(m.ReceivableInput(row.source_key, m._hash(asdict(row)), 'matched', value,
            'a' * 64, date(2024, 5, 1)))
    return replace(data, receivables=tuple(evidence))


def build(data, cutoff=date(2024, 5, 31)):
    return m.build_enterprise_analysis(data, cutoff, m.EnterprisePolicy(min_samples=1))


def white(result):
    return next(c for c in result['cards'] if c['id'] == 'white-strip')


def test_white_strip_group_totals_not_mean_and_existing_cards_unchanged():
    data = inputs(); original = repr(data)
    old = build(data)
    result = build(enabled(data))
    assert result['algorithmVersion'] == 'enterprise-quarter-v2'
    assert result['cards'][:2] == old['cards']
    assert [c['id'] for c in result['cards']] == ['scissors', 'inventory', 'white-strip']
    p = white(result)['points'][-1]
    assert (p['primary'], p['secondary'], p['result']) == pytest.approx((14, 10, 4))
    assert (p['sampleCount'], p['candidateCount'], p['coverage']) == (2, 2, 100)
    q = white(result)['quality'][-1]
    assert any(ref['table'] == 'receivable-supplement' for ref in q['sourceRefs'])
    assert result['inputHash'] != old['inputHash'] and repr(data) == original


@pytest.mark.parametrize('bad,reason', [('future', 'supplement-not-yet-visible'),
    ('binding', 'supplement-source-changed'), ('null', 'returned-null'), ('invalid', 'missing-or-invalid-field')])
def test_bad_supplement_stays_missing_in_denominator(bad, reason):
    data = enabled(); evidence = list(data.receivables)
    index = next(i for i, r in enumerate(evidence) if r.source_key == 'balance:000001.SZ:2024')
    changes = {'future': {'available_date': date(2024, 6, 1)}, 'binding': {'source_hash': 'b' * 64},
               'null': {'status': 'returned-null', 'value': None}, 'invalid': {'value': 'NaN'}}[bad]
    evidence[index] = replace(evidence[index], **changes)
    c = white(build(replace(data, receivables=tuple(evidence))))
    assert c['points'] == []
    q = c['quality'][-1]
    assert (q['sampleCount'], q['candidateCount'], q['status']) == (1, 2, 'insufficient-coverage')
    assert q['excluded']['000001.SZ'] == reason


def test_existing_core_null_cannot_be_overridden_by_valid_supplement():
    data = inputs()
    data = replace(data, financials=tuple(replace(r, values={**r.values, 'accounts_receiv': None})
        if r.source_key == 'balance:000001.SZ:2024' else r for r in data.financials))
    q = white(build(enabled(data)))['quality'][-1]
    assert q['sampleCount'] == 1 and q['excluded']['000001.SZ'] == 'missing-or-invalid-field'


@pytest.mark.parametrize('base', ['0', '-100'])
def test_nonpositive_group_receivables_base_does_not_emit_point(base):
    data = enabled()
    data = replace(data, receivables=tuple(replace(r, value=base) if ':2023' in r.source_key else r for r in data.receivables))
    c = white(build(data))
    assert c['points'] == [] and c['quality'][-1]['status'] == 'non-positive-aggregate-base'
    assert c['quality'][-1]['sampleCount'] == 2


def test_q2_revenue_is_single_quarter_but_receivables_are_end_stock():
    data = inputs(); rows = list(data.financials)
    for symbol, old_rev, new_rev, old_ar, new_ar in (
        ('000001.SZ', 250, 300, 400, 500), ('000002.SZ', 2250, 2700, 600, 900)):
        for year, revenue, ar in ((2023, old_rev, old_ar), (2024, new_rev, new_ar)):
            period = date(year, 6, 30)
            for table, values in (('income', {'revenue': revenue, 'n_income_attr_p': 20}),
                                  ('balance', {'inventories': 100, 'accounts_receiv': ar})):
                rows.append(m.FinancialRecord(table, symbol, period, period + timedelta(days=30), None,
                    '1', '1', '1', values, f'q2:{table}:{symbol}:{year}'))
    data = enabled(replace(data, financials=tuple(rows)))
    p = white(build(data, date(2024, 8, 31)))['points'][-1]
    assert p['period'] == '2024-06-30'
    assert (p['primary'], p['secondary'], p['result']) == pytest.approx((40, 26.6666666667, 13.3333333333))
