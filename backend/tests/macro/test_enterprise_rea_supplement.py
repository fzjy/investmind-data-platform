"""补充只能改变REA，不能改旧结果或把null当0。"""
from dataclasses import asdict, replace
from datetime import date
import importlib

import pytest

from app.macro.enterprise import EnterprisePolicy, _hash
from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis
from backend.tests.macro.test_enterprise_rea import controlled, metric


AS_OF = date(2024, 8, 1)
POLICY = EnterprisePolicy(min_samples=1)
FIELDS = {'balance': ('fix_assets', 'acct_payable'),
          'cashflow': ('depr_fa_coga_dpba', 'amort_intang_assets', 'lt_amort_deferred_exp', 'c_paid_to_for_empl')}


def supplemented(count=2):
    module = importlib.import_module('app.macro.enterprise')
    assert hasattr(module, 'ReaFieldInput'), 'REA lacks independent field evidence inputs'
    original = controlled(count)
    rows = []
    evidence = []
    for row in original.financials:
        fields = FIELDS.get(row.table, ())
        missing = replace(row, values={k: v for k, v in row.values.items() if k not in fields})
        rows.append(missing)
        for field in fields:
            evidence.append(module.ReaFieldInput(row.source_key, _hash(asdict(missing)), field, 'matched',
                str(row.values[field]), _hash([row.source_key, field]), date(2024, 7, 20),
                _hash(row.source_key)))
    base = replace(original, financials=tuple(rows))
    return base, replace(base, rea_fields=tuple(evidence))


def build(inputs):
    return public_enterprise_analysis(inputs, AS_OF, POLICY, rea_enabled=True)


def test_supplement_restores_correct_single_quarter_values_without_changing_legacy():
    base, inputs = supplemented(61)
    old, new = build(base), build(inputs)
    assert {k: v for k, v in old.items() if k != 'rea'} == {k: v for k, v in new.items() if k != 'rea'}
    assert new['poolCount'] == 61 and valid_enterprise_analysis(new)
    for identifier in ('capacity', 'renewal', 'employee', 'payable'):
        assert metric(old['rea'], identifier)['points'] == []
        assert metric(new['rea'], identifier)['points'][-1]['sampleCount'] == 61
    point = metric(new['rea'], 'renewal')['points'][-1]
    assert point['amounts'] == {'a': 1220, 'b': 610, 'oldA': 1220, 'oldB': 610}
    assert point['value'] == 2
    assert metric(new['rea'], 'employee')['points'][-1]['value'] == 0
    assert metric(new['rea'], 'payable')['points'][-1]['value'] == 0
    assert metric(old['rea'], 'renewal')['quality'][-1]['provenanceHash'] != metric(new['rea'], 'renewal')['quality'][-1]['provenanceHash']


@pytest.mark.parametrize('change,reason', [
    ({'status': 'returned-null', 'value': None}, 'rea-supplement-returned-null'),
    ({'available_date': date(2024, 8, 2)}, 'rea-supplement-not-visible'),
    ({'source_hash': '0' * 64}, 'rea-supplement-source-mismatch'),
    ({'status': 'invalid-value', 'value': None}, 'rea-supplement-invalid-value'),
    ({'value': '-1'}, 'negative-field:fix_assets'),
])
def test_unusable_field_evidence_remains_an_explicit_gap(change, reason):
    _, inputs = supplemented()
    inputs = replace(inputs, rea_fields=tuple(replace(e, **change) if e.field == 'fix_assets' else e for e in inputs.rea_fields))
    result = metric(build(inputs)['rea'], 'capacity')
    assert result['points'] == []
    assert set(result['quality'][-1]['excluded'].values()) == {reason}


def test_existing_null_and_zero_are_not_overwritten_and_duplicate_evidence_is_rejected():
    _, inputs = supplemented()
    for existing in (None, 0):
        changed = replace(inputs, financials=tuple(replace(r, values={**r.values, 'fix_assets': existing})
            if r.table == 'balance' else r for r in inputs.financials))
        result = metric(build(changed)['rea'], 'capacity')
        if existing is None:
            assert result['points'] == []
            assert set(result['quality'][-1]['excluded'].values()) == {'missing-field:fix_assets'}
        else:
            assert result['points'][-1]['value'] is None
            assert result['points'][-1]['amounts']['b'] == 0
    with pytest.raises(ValueError, match='duplicate REA'):
        build(replace(inputs, rea_fields=inputs.rea_fields + inputs.rea_fields[:1]))


def test_one_missing_depreciation_component_is_not_summed_as_zero():
    _, inputs = supplemented()
    inputs = replace(inputs, rea_fields=tuple(e for e in inputs.rea_fields if e.field != 'lt_amort_deferred_exp'))
    result = metric(build(inputs)['rea'], 'renewal')
    assert result['points'] == []
    assert set(result['quality'][-1]['excluded'].values()) == {'missing-field:lt_amort_deferred_exp'}
