"""防止跨日并集、静默删坏行及把候选数量误当完整性。"""
from dataclasses import replace
from datetime import date, datetime, timezone
from importlib import import_module

import pytest

from app.connectors.tushare.contracts import TushareResponse
from backend.tests.macro.test_target_evidence_policy import policy


CAPTURED = datetime(2024, 8, 20, 9, tzinfo=timezone.utc)
AS_OF = date(2024, 8, 20)
FIELDS = {
    'index_weight': ('index_code', 'con_code', 'trade_date', 'weight'),
    'index_basic': ('ts_code', 'name', 'fullname', 'market', 'publisher', 'exp_date'),
    'etf_basic': ('ts_code', 'csname', 'index_code', 'index_name', 'exchange', 'list_status', 'etf_type'),
}


def contract():
    try:
        return import_module('app.macro.target_evidence_contract')
    except ImportError:
        pytest.fail('target evidence request contract missing')


def request(api='index_weight', *, target='000300.SH', kind='index', params=None, fields=None):
    defaults = {
        'index_weight': {'index_code': '000300.SH', 'start_date': '20240801', 'end_date': '20240820'},
        'index_basic': {'ts_code': '000300.SH', 'market': 'CSI'},
        'etf_basic': {'ts_code': target},
    }
    return contract().TargetEvidenceRequest(target, kind, api,
        defaults.get(api, {}) if params is None else params, FIELDS.get(api, ()) if fields is None else fields)


def response(api='index_weight', rows=None, fields=None):
    defaults = {
        'index_weight': [('000300.SH', '000001.SZ', '20240820', 0)],
        'index_basic': [('000300.SH', '沪深300', '沪深300指数', 'CSI', '中证指数', None)],
        'etf_basic': [('512880.SH', '证券ETF', '399975.SZ', '证券公司', 'SH', 'L', '境内')],
    }
    # model_construct保留坏标量原类型，避免依赖Pydantic替本模块校验。
    return TushareResponse.model_construct(fields=FIELDS[api] if fields is None else tuple(fields),
        items=tuple(tuple(r) for r in (defaults[api] if rows is None else rows)))


def normalize(req=None, res=None, **kwargs):
    try:
        mod = import_module('app.macro.target_evidence_normalize')
    except ImportError:
        pytest.fail('target evidence normalizer missing')
    args = dict(captured_at=CAPTURED, as_of=AS_OF, policy=policy(), timezone='Asia/Shanghai')
    args.update(kwargs)
    return mod.normalize_target_response(request() if req is None else req,
        response() if res is None else res, **args)


def test_dates_stay_separate_zero_weight_preserved_and_sort_is_stable():
    rows = [('000300.SH', c, d, w) for c, d, w in [
        ('000003.SZ', '20240820', 2.5), ('000002.SZ', '20240801', 3),
        ('000001.SZ', '20240801', 0), ('000002.SZ', '20240820', 1),
        ('000001.SZ', '20240820', 0)]]
    value = normalize(res=response(rows=rows))
    assert value['qualityStatus'] == 'valid_candidate'
    groups = value['snapshotCandidates']
    assert [(g['tradeDate'], g['observedMemberCount']) for g in groups] == [('2024-08-01', 2), ('2024-08-20', 3)]
    assert groups[0]['members'] == [{'code': '000001.SZ', 'weight': '0'}, {'code': '000002.SZ', 'weight': '3'}]
    fields = tuple(reversed(FIELDS['index_weight']))
    other = normalize(res=response(rows=[tuple(reversed(r)) for r in reversed(rows)], fields=fields))
    assert [g['membersHash'] for g in groups] == [g['membersHash'] for g in other['snapshotCandidates']]
    assert value['responseHash'] != other['responseHash']
    assert value['contractVersion'] == 'enterprise-target-candidate-v1'
    assert value['capturedAt'] == '2024-08-20T09:00:00+00:00' and value['asOf'] == '2024-08-20'


@pytest.mark.parametrize('weight', [-1, 101, float('nan'), float('inf'), True, '0', None])
def test_any_bad_weight_rejects_entire_response_not_older_fallback(weight):
    value = normalize(res=response(rows=[('000300.SH', '000001.SZ', '20240801', 1),
        ('000300.SH', '000002.SZ', '20240820', weight)]))
    assert value['qualityStatus'] == 'rejected' and value['snapshotCandidates'] == []
    assert value['reasons'] and value['membershipReady'] is False


@pytest.mark.parametrize('index,code,day', [
    ('000300.CSI', '000001.SZ', '20240820'), ('000300.SH', 'CASH', '20240820'),
    ('000300.SH', 'AAPL', '20240820'), ('000300.SH', '000001', '20240820'),
    ('000300.SH', '000001.SZ', '20240821'), ('000300.SH', '000001.SZ', '20240731'),
    ('000300.SH', '000001.SZ', '20240230'), ('000300.SH', '000001.SZ', '2024-08-01'),
])
def test_bad_source_member_or_date_rejects_all(index, code, day):
    value = normalize(res=response(rows=[(index, code, day, 1)]))
    assert value['qualityStatus'] == 'rejected' and value['snapshotCandidates'] == []


def test_duplicate_same_day_member_is_not_deduplicated():
    value = normalize(res=response(rows=[('000300.SH', '000001.SZ', '20240820', 1)] * 2))
    assert value['qualityStatus'] == 'rejected' and not value['snapshotCandidates']


@pytest.mark.parametrize('count', [61, 300])
def test_pool_outside_members_and_total_weight_cannot_prove_completeness(count):
    rows = [('000300.SH', f'{i:06}.BJ', '20240820', 100 if i == 1 else 0) for i in range(1, count + 1)]
    value = normalize(res=response(rows=rows), policy=replace(policy(), collection_enabled=True))
    assert value['snapshotCandidates'][0]['observedMemberCount'] == count
    assert value['completeness'] == 'not_proven' and value['sourceConstituentCount'] is None
    assert value['membershipReady'] is False and value['notStrictPointInTime'] is True


@pytest.mark.parametrize('fields,rows', [
    (('index_code', 'con_code', 'trade_date', 'trade_date'), [('000300.SH', '000001.SZ', '20240820', 1)]),
    (('index_code', 'con_code', 'trade_date'), [('000300.SH', '000001.SZ', '20240820')]),
    (FIELDS['index_weight'] + ('extra',), [('000300.SH', '000001.SZ', '20240820', 1, None)]),
    (FIELDS['index_weight'], [('000300.SH', '000001.SZ', '20240820')]),
])
def test_structural_mismatch_fails_before_zip(fields, rows):
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_INVALID$'):
        normalize(res=response(fields=fields, rows=rows))


@pytest.mark.parametrize('limit', ['rows', 'members'])
def test_resource_limits_do_not_silently_truncate(limit):
    p = replace(policy(), **({'max_response_rows': 1} if limit == 'rows' else {'max_snapshot_members': 1}))
    rows = [('000300.SH', '000001.SZ', '20240820', 1), ('000300.SH', '000002.SZ', '20240820', 1)]
    if limit == 'rows':
        with pytest.raises(ValueError, match='^TARGET_EVIDENCE_INVALID$'):
            normalize(res=response(rows=rows), policy=p)
    else:
        assert normalize(res=response(rows=rows), policy=p)['qualityStatus'] == 'rejected'


@pytest.mark.parametrize('args', [
    {'captured_at': datetime(2024, 8, 20)}, {'captured_at': datetime(2024, 8, 19, 1, tzinfo=timezone.utc)},
    {'timezone': 'invalid/not-zone'}, {'as_of': CAPTURED}, {'policy': None},
    {'policy': replace(policy(), max_response_rows=True)},
])
def test_invalid_time_or_policy_is_safe_error(args):
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_INVALID$'):
        normalize(**args)


@pytest.mark.parametrize('api,target,kind', [('index_basic', '000300.SH', 'index'), ('etf_basic', '512880.SH', 'etf')])
def test_metadata_requires_exactly_one_and_does_not_guess_alias(api, target, kind):
    req = request(api, target=target, kind=kind)
    assert normalize(req, response(api))['qualityStatus'] == 'valid_candidate'
    assert normalize(req, response(api, rows=[]))['qualityStatus'] == 'unavailable'
    rows = list(response(api).items)
    assert normalize(req, response(api, rows=rows * 2))['qualityStatus'] == 'rejected'
    row = list(rows[0]); row[0] = '000300.CSI'
    assert normalize(req, response(api, rows=[row]))['qualityStatus'] == 'rejected'


@pytest.mark.parametrize('field,bad,status', [
    ('market', 'SSE', 'rejected'), ('publisher', '', 'rejected'), ('name', None, 'rejected'),
    ('exp_date', '20240820', 'unavailable'), ('exp_date', '20240230', 'rejected'),
])
def test_index_metadata_market_and_expiration(field, bad, status):
    row = list(response('index_basic').items[0]); row[FIELDS['index_basic'].index(field)] = bad
    assert normalize(request('index_basic'), response('index_basic', [row]))['qualityStatus'] == status


@pytest.mark.parametrize('field,bad,status', [
    ('list_status', 'D', 'unavailable'), ('list_status', 'P', 'unavailable'),
    ('list_status', 'UNKNOWN', 'rejected'), ('etf_type', 'QDII', 'unsupported'),
    ('etf_type', None, 'rejected'), ('exchange', 'SZ', 'rejected'),
    ('index_code', 'https://private', 'rejected'), ('index_code', '', 'rejected'),
    ('index_name', '', 'rejected'),
])
def test_etf_mapping_lifecycle_and_market(field, bad, status):
    row = list(response('etf_basic').items[0]); row[FIELDS['etf_basic'].index(field)] = bad
    value = normalize(request('etf_basic', target='512880.SH', kind='etf'), response('etf_basic', [row]))
    assert value['qualityStatus'] == status and value['membershipReady'] is False


@pytest.mark.parametrize('kwargs', [
    {'api': 'fund_portfolio'}, {'target': '000300'}, {'kind': 'fund'},
    {'params': {'index_code': '000300.SH', 'start_date': '20240230', 'end_date': '20240820'}},
    {'params': {'index_code': '000300.SH', 'start_date': '20240820', 'end_date': '20240801'}},
    {'params': {'index_code': 'private url', 'start_date': '20240801', 'end_date': '20240820'}},
    {'params': {'index_code': '000300.SH', 'start_date': '20240801', 'end_date': '20240820', 'extra': 1}},
    {'fields': tuple(reversed(FIELDS['index_weight']))},
    {'api': 'etf_basic', 'kind': 'index'},
])
def test_request_rejects_unapproved_api_shape_and_identity(kwargs):
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_INVALID$'):
        request(**kwargs)


def test_request_copies_params_and_binds_original_etf_identity():
    params = {'index_code': '399975.SZ', 'start_date': '20240801', 'end_date': '20240820'}
    req = request(target='512880.SH', kind='etf', params=params)
    digest = contract().target_request_hash(req)
    params['index_code'] = '000300.SH'
    assert req.params['index_code'] == '399975.SZ' and contract().target_request_hash(req) == digest
    with pytest.raises(TypeError):
        req.params['index_code'] = '000300.SH'
    assert contract().target_request_hash(request(target='512800.SH', kind='etf', params=dict(req.params))) != digest


@pytest.mark.parametrize('api', ['index_basic', 'index_weight'])
@pytest.mark.parametrize('source', ['000905.SH', '000300.CSI'])
def test_direct_index_cannot_bind_other_index_or_guessed_alias(api, source):
    params = {'ts_code': source, 'market': 'CSI'} if api == 'index_basic' else {
        'index_code': source, 'start_date': '20240801', 'end_date': '20240820'}
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_INVALID$'):
        request(api, target='000300.SH', kind='index', params=params)


@pytest.mark.parametrize('start,end', [('20240801', '20240920'), ('20240101', '20240131')])
def test_empty_response_does_not_bypass_frozen_window_validation(start, end):
    req = request(params={'index_code': '000300.SH', 'start_date': start, 'end_date': end})
    with pytest.raises(ValueError, match='^TARGET_EVIDENCE_INVALID$'):
        normalize(req, response(rows=[]))


def test_empty_valid_window_is_unavailable_not_invalid_or_complete():
    value = normalize(res=response(rows=[]))
    assert value['qualityStatus'] == 'unavailable' and value['snapshotCandidates'] == []
    assert value['membershipReady'] is False
