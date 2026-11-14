"""防止目标身份猜测、损坏配置被吞掉或配置指纹失真。"""
from copy import deepcopy
from importlib import import_module
import json

import pytest


def module():
    try:
        return import_module('app.macro.enterprise_target_catalog')
    except ImportError:
        pytest.fail('enterprise target catalog is not implemented')


def policy_value():
    return {'collectionEnabled': False, 'useScope': 'local-development-only',
        'bootstrapTargetCodes': ['000300.SH', '512880.SH'], 'maxTargetsPerPlan': 2,
        'maxHttpPerRun': 2, 'maxHttpPerPlan': 12, 'requestsPerMinute': 10,
        'requestTimeoutSeconds': 20, 'maxRetries': 0, 'lookbackMonths': 3,
        'indexMetadataMarket': 'CSI', 'maxResponseBytes': 8388608,
        'maxResponseRows': 20000, 'maxSnapshotMembers': 10000}


def payload():
    return {
        'schemaVersion': 2, 'catalogVersion': 'controlled-v2',
        'mode': 'tracking-index-proxy', 'maxTargetsPerPreview': 2,
        'evidencePolicy': policy_value(),
        'targets': [
            {'kind': 'index', 'code': '000300.SH', 'label': '沪深300', 'sourceIndexNameHint': None},
            {'kind': 'etf', 'code': '512880.SH', 'label': '证券ETF', 'sourceIndexNameHint': '中证全指证券公司指数'},
        ],
    }


def load(tmp_path, value):
    file = tmp_path / 'targets.json'
    file.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return module().load_enterprise_target_catalog(file)


def test_default_catalog_drives_all_original_targets_without_guessed_mapping():
    value = module().load_enterprise_target_catalog()
    assert [t.code for t in value.targets if t.kind == 'index'] == ['000300.SH', '000905.SH', '000510.SH']
    assert [t.code for t in value.targets if t.kind == 'etf'] == [
        '512880.SH', '512800.SH', '159928.SZ', '512010.SH', '512400.SH', '512660.SH',
        '515220.SH', '512980.SH', '515030.SH', '512200.SH', '588200.SH',
    ]
    assert value.targets[3].source_index_name_hint == '中证全指证券公司指数'
    assert len(value.fingerprint) == 64
    assert not hasattr(value.targets[3], 'source_index_code')


@pytest.mark.parametrize(('where', 'key', 'bad'), [
    ('root', 'schemaVersion', True), ('root', 'schemaVersion', 1), ('root', 'schemaVersion', 2.0),
    ('root', 'catalogVersion', ''), ('root', 'catalogVersion', '  '),
    ('root', 'mode', 'fund-holdings'), ('root', 'maxTargetsPerPreview', True),
    ('root', 'maxTargetsPerPreview', 0), ('root', 'maxTargetsPerPreview', -1),
    ('root', 'maxTargetsPerPreview', 1), ('root', 'maxTargetsPerPreview', 2.0),
    ('root', 'extra', 'private'), ('root', 'targets', []), ('root', 'targets', {}),
    ('target', 'kind', 'industry'), ('target', 'code', '000300'),
    ('target', 'code', '000300.CSI'), ('target', 'code', ' 000300.SH'),
    ('target', 'label', ''), ('target', 'label', ' '), ('target', 'label', 1),
    ('target', 'sourceIndexNameHint', 'unexpected'), ('target', 'extra', True),
    ('etf', 'sourceIndexNameHint', None), ('etf', 'sourceIndexNameHint', ' '),
])
def test_bad_catalog_rejected_without_coercion(tmp_path, where, key, bad):
    value = payload()
    target = value if where == 'root' else value['targets'][where == 'etf']
    target[key] = bad
    with pytest.raises(ValueError):
        load(tmp_path, value)


def test_duplicate_targets_are_rejected_even_with_different_kind(tmp_path):
    value = payload()
    value['targets'][1]['code'] = '000300.SH'
    with pytest.raises(ValueError):
        load(tmp_path, value)


@pytest.mark.parametrize('text', [
    '{"schemaVersion":1,"schemaVersion":2}',
    json.dumps(payload()).replace('"kind": "index"', '"kind":"etf","kind":"index"'),
    json.dumps(payload()).replace('"maxTargetsPerPreview": 2', '"maxTargetsPerPreview": NaN'),
    'null', '[]', 'true', '{',
])
def test_duplicate_json_keys_and_malformed_documents_are_rejected(tmp_path, text):
    file = tmp_path / 'bad.json'
    file.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError):
        module().load_enterprise_target_catalog(file)


def test_fingerprint_ignores_json_whitespace_not_target_identity(tmp_path):
    value = payload()
    first = load(tmp_path, value)
    file = tmp_path / 'formatted.json'
    file.write_text(json.dumps(value, indent=3, sort_keys=True), encoding='utf-8')
    assert module().load_enterprise_target_catalog(file).fingerprint == first.fingerprint
    changed = deepcopy(value)
    changed['targets'][0]['code'] = '000905.SH'
    changed['evidencePolicy']['bootstrapTargetCodes'][0] = '000905.SH'
    assert load(tmp_path, changed).fingerprint != first.fingerprint


def test_full_v1_catalog_rejected_without_rewriting(tmp_path):
    value = payload()
    value['schemaVersion'] = 1
    del value['evidencePolicy']
    file = tmp_path / 'legacy.json'
    file.write_text(json.dumps(value), encoding='utf-8')
    before = file.read_bytes()
    with pytest.raises(ValueError, match='^ENTERPRISE_TARGET_CATALOG_INVALID$'):
        module().load_enterprise_target_catalog(file)
    assert file.read_bytes() == before


@pytest.mark.parametrize(('field', 'bad', 'delete'), [
    ('schemaVersion', 1, False), ('schemaVersion', 3, False),
    ('schemaVersion', None, True), ('schemaVersion', True, False),
    ('schemaVersion', 2.0, False), ('schemaVersion', '2', False),
    ('evidencePolicy', None, True), ('evidencePolicy', None, False),
    ('evidencePolicy', {}, False),
])
def test_catalog_requires_exact_v2_and_policy(tmp_path, field, bad, delete):
    value = payload()
    if delete:
        del value[field]
    else:
        value[field] = bad
    with pytest.raises(ValueError):
        load(tmp_path, value)
