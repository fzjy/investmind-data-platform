"""纯内存事件合同：防止提前计现、误合并事件及以缺失冒充零。"""
from copy import deepcopy
from datetime import date
from decimal import Decimal
from itertools import permutations
import importlib

import pytest


def resolve(rows, as_of=date(2026, 10, 30)):
    # 延迟导入使未实现合同的 RED 体现为用例失败。
    return importlib.import_module('app.portfolio.dividend_contract').resolve_dividend_events(rows, as_of)


def row(**changes):
    value = dict(ts_code='600001.SH', end_date='20251231', ann_date='20260501',
                 div_proc='实施', cash_div_tax='0.5', record_date='20260510',
                 ex_date='20260511', pay_date='20260512', sourceId='raw:1',
                 data=dict(imp_ann_date='20260501', stk_div='0', stk_bo_rate='0',
                           stk_co_rate='0', base_date='20251231', base_share='100'))
    value.update(changes)
    return value


def codes(result):
    return {issue['code'] for issue in result['issues']}


@pytest.mark.parametrize('stage', ['终止实施', '取消实施', '不实施'])
def test_additional_exact_cancellation_labels_invalidate_old_paid_revision(stage):
    result = resolve([row(), row(div_proc=stage, ann_date='20260601')])
    assert result['events'] == []


@pytest.mark.parametrize('changes', [
    {'record_date':'20260515'}, {'pay_date':'20260509'}, {'ex_date':'20260513'},
])
def test_impossible_eligibility_and_payment_chronology_is_not_cash(changes):
    assert resolve([row(**changes)])['events'] == []


def test_positive_decimal_underflow_cannot_become_known_zero():
    result = resolve([row(cash_div_tax=Decimal('1e-400'))])
    assert result['events'] == [] and result['issues']


def test_exact_decimal_difference_is_not_merged_before_conflict_check():
    result=resolve([row(cash_div_tax='999999999999.12345678'),row(cash_div_tax='999999999999.12345679',sourceId='other')])
    assert result['events']==[] and 'same_day_conflict' in codes(result)


def test_tiny_positive_capital_is_share_changing_not_zero():
    value=row();value['data']['stk_div']='1e-400'
    result=resolve([value])
    assert not result['events'] or result['events'][0]['capitalStatus']!='unchanged-distribution'


@pytest.mark.parametrize('stage', ['实施', '实施完成'])
def test_exact_completed_stages_and_normalized_facts(stage):
    result = resolve([row(div_proc=stage, ann_date=date(2026, 5, 1), cash_div_tax=Decimal('0.5'))])
    assert result['status'] == 'ok'
    assert result['events'] == [dict(ts_code='600001.SH', end_date='2025-12-31',
        ann_date='2026-05-01', record_date='2026-05-10', ex_date='2026-05-11',
        pay_date='2026-05-12', imp_ann_date='2026-05-01', cash_div_tax=0.5,
        sourceIds=['raw:1'], capitalStatus='unchanged-distribution')]


@pytest.mark.parametrize('stage', ['预案', '股东大会通过', '未实施', '实施中', '实施完成后', '', None])
def test_unknown_or_proposal_stage_never_counts_cash(stage):
    result = resolve([row(div_proc=stage)])
    assert result['events'] == []
    assert result['issues']


@pytest.mark.parametrize('field', ['ann_date', 'imp_ann_date', 'pay_date', 'record_date', 'ex_date'])
def test_future_dates_do_not_count_cash(field):
    value = row()
    (value['data'] if field == 'imp_ann_date' else value)[field] = '20261101'
    result = resolve([value])
    assert result['events'] == []
    assert 'future_date' in codes(result)


def test_same_year_multiple_economic_events_survive():
    second = row(record_date='20260810', ex_date='20260811', pay_date='20260812', sourceId='raw:2')
    assert len(resolve([row(), second])['events']) == 2


@pytest.mark.parametrize('field', ['ex_date', 'pay_date'])
def test_same_announcement_distinct_dates_are_not_arbitrarily_resolved(field):
    result = resolve([row(), row(**{field: '20260513', 'sourceId': 'other'})])
    assert result['events'] == []
    assert 'same_day_conflict' in codes(result)


def test_latest_revision_and_duplicate_lineage_are_order_independent():
    latest = row(ann_date='20260502', cash_div_tax='0.7', sourceId=42)
    duplicate = deepcopy(latest)
    duplicate['sourceId'] = 'raw:copy'
    for rows in permutations([row(), latest, duplicate]):
        event = resolve(rows)['events'][0]
        assert event['cash_div_tax'] == 0.7
        assert event['ann_date'] == '2026-05-02'
        assert event['sourceIds'] == ['42', 'raw:copy']


def test_future_revision_does_not_hide_known_event():
    result = resolve([row(), row(ann_date='20261101', cash_div_tax='1')])
    assert result['events'][0]['cash_div_tax'] == 0.5


@pytest.mark.parametrize('change', [dict(cash_div_tax='0.6'), dict(data=dict(
    imp_ann_date='20260501', stk_div='0.1', stk_bo_rate='0.1', stk_co_rate='0')),
    dict(data=dict(imp_ann_date='20260502', stk_div='0', stk_bo_rate='0', stk_co_rate='0'))])
def test_same_day_conflicts_block_event(change):
    result = resolve([row(), row(sourceId='other', **change)])
    assert result['events'] == []
    assert 'same_day_conflict' in codes(result)


@pytest.mark.parametrize('stage', ['取消', '停止', '停止实施'])
def test_latest_cancellation_removes_only_located_event(stage):
    second = row(record_date='20260810', ex_date='20260811', pay_date='20260812')
    result = resolve([row(), second, row(ann_date='20260601', div_proc=stage)])
    assert len(result['events']) == 1
    assert result['events'][0]['pay_date'] == '2026-08-12'


def test_unlocated_cancellation_blocks_security_fiscal_year_only():
    cancel = row(ann_date='20260601', div_proc='停止实施', record_date=None, ex_date=None, pay_date=None)
    result = resolve([row(), cancel, row(ts_code='OTHER')])
    assert [event['ts_code'] for event in result['events']] == ['OTHER']
    assert 'unlocated_cancellation' in codes(result)


def test_proposal_does_not_supersede_implemented_cash():
    result = resolve([row(), row(ann_date='20260601', div_proc='预案')])
    assert result['events'][0]['cash_div_tax'] == 0.5


def test_missing_implementation_announcement_blocks_legacy_cash():
    result = resolve([row(data={})])
    assert result['events'] == []
    assert 'missing_imp_ann_date' in codes(result)


def test_missing_pretax_cash_never_falls_back_to_aftertax():
    result = resolve([row(cash_div_tax=None, cash_div='0.4')])
    assert result['events'] == []
    assert 'missing_cash_div_tax' in codes(result)


@pytest.mark.parametrize('data,status', [
    (dict(imp_ann_date='20260501'), 'missing'),
    (dict(imp_ann_date='20260501', stk_div='', stk_bo_rate=None, stk_co_rate=''), 'missing'),
    (dict(imp_ann_date='20260501', stk_div='0', stk_bo_rate='0', stk_co_rate='0'), 'unchanged-distribution'),
    (dict(imp_ann_date='20260501', stk_div='0.1'), 'share-changing'),
    (dict(imp_ann_date='20260501', stk_co_rate='0.2'), 'share-changing'),
])
def test_capital_evidence_is_distribution_local(data, status):
    result = resolve([row(data=data, cash_div_tax=0)])
    assert result['events'][0]['capitalStatus'] == status
    assert result['events'][0]['cash_div_tax'] == 0.0
    if status == 'missing':
        assert 'missing_capital' in codes(result)


@pytest.mark.parametrize('change,code', [
    (dict(ts_code=None), 'missing_symbol'), (dict(ann_date='20260230'), 'invalid_date'),
    (dict(end_date='bad'), 'invalid_date'), (dict(cash_div_tax=-1), 'invalid_number'),
    (dict(cash_div_tax=float('nan')), 'invalid_number'), (dict(cash_div_tax='Infinity'), 'invalid_number'),
    (dict(data=dict(imp_ann_date='bad')), 'invalid_date'),
    (dict(data=dict(imp_ann_date='20260501', stk_div=-1)), 'invalid_number'),
    (dict(data=dict(imp_ann_date='20260501', base_share='NaN')), 'invalid_number'),
    (dict(data=dict(imp_ann_date='20260501', base_date='bad')), 'invalid_date'),
])
def test_bad_input_is_explained_and_never_produces_cash(change, code):
    result = resolve([row(**change)])
    assert result['events'] == []
    assert code in codes(result)


def test_raw_mapping_and_implicit_source_identity_are_stable_without_mutation():
    raw = row()
    raw.update(raw.pop('data'))
    raw.pop('sourceId')
    before = deepcopy(raw)
    first = resolve([raw, deepcopy(raw)])
    assert raw == before
    assert len(first['events']) == 1
    assert len(first['events'][0]['sourceIds']) == 1
    assert isinstance(first['events'][0]['sourceIds'][0], str)
    assert first == resolve([deepcopy(raw)])


def test_no_hardcoded_event_or_source_limit():
    rows = [row(ts_code=f'S{i}', sourceId=i) for i in range(75)]
    result = resolve(rows)
    assert len(result['events']) == 75
    assert resolve([]) == dict(events=[], issues=[], status='empty')


def test_unlocated_cancellation_blocks_all_reporting_periods_in_fiscal_year():
    cancel = row(ann_date='20260901', div_proc='停止实施', record_date=None, ex_date=None, pay_date=None)
    interim = row(end_date='20250630', record_date='20260810', ex_date='20260811', pay_date='20260812')
    result = resolve([row(), interim, cancel])
    assert result['events'] == []
    assert 'unlocated_cancellation' in codes(result)


def test_historical_same_day_conflict_remains_explained_after_revision():
    result = resolve([row(), row(cash_div_tax='0.6'), row(ann_date='20260502', cash_div_tax='0.7')])
    assert result['events'][0]['cash_div_tax'] == 0.7
    assert 'same_day_conflict' in codes(result)


def test_latest_known_future_payment_blocks_old_version():
    result = resolve([row(), row(ann_date='20260502', pay_date='20261101')])
    assert result['events'] == []
    assert 'future_date' in codes(result)


def test_latest_malformed_revision_does_not_fall_back():
    result = resolve([row(), row(ann_date='20260502', cash_div_tax='NaN')])
    assert result['events'] == []
    assert 'invalid_number' in codes(result)


def test_as_of_is_inclusive_and_zero_is_not_positive_cash():
    event = resolve([row(cash_div_tax='0')], date(2026, 5, 12))['events'][0]
    assert isinstance(event['cash_div_tax'], float)
    assert event['cash_div_tax'] == 0.0
    assert not event['cash_div_tax'] > 0


def test_ambiguous_partial_cancellation_blocks_year_instead_of_picking():
    first = row()
    second = row(record_date='20260810', ex_date='20260811', pay_date='20260812')
    bridge = row(ann_date='20260901', div_proc='停止实施', record_date=None, ex_date=None, pay_date=None)
    result = resolve([first, second, bridge])
    assert result['events'] == []
    assert 'unlocated_cancellation' in codes(result)


def test_payment_revision_keeps_one_event_and_latest_source_identity():
    revised = row(ann_date='20260502', pay_date='20260520', sourceId='core.dividend:2')
    result = resolve([row(sourceId='core.dividend:1'), revised])
    assert len(result['events']) == 1
    assert result['events'][0]['pay_date'] == '2026-05-20'
    assert result['events'][0]['sourceIds'] == ['core.dividend:2']


def test_different_record_dates_survive_even_when_ex_and_pay_dates_match():
    result = resolve([row(), row(record_date='20260509', sourceId='core.dividend:2')])
    assert len(result['events']) == 2
    assert {event['record_date'] for event in result['events']} == {'2026-05-09', '2026-05-10'}


def test_missing_record_date_can_use_ex_date_without_inventing_record_date():
    result = resolve([row(record_date=None), row(record_date=None, ann_date='20260502', pay_date='20260520')])
    assert len(result['events']) == 1
    assert result['events'][0]['record_date'] is None
    assert result['events'][0]['pay_date'] == '2026-05-20'


def test_payment_date_alone_is_not_an_event_identity():
    result = resolve([row(record_date=None, ex_date=None)])
    assert result['events'] == []
    assert 'missing_event_identity' in codes(result)
