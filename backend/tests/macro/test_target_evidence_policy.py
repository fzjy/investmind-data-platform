from copy import deepcopy
from hashlib import sha256
from importlib import import_module
import json

import pytest

from backend.tests.macro.test_enterprise_target_catalog import payload, load, policy_value


def module():
    try:
        return import_module('app.macro.target_evidence_policy')
    except ImportError:
        pytest.fail('target evidence policy missing')


def policy():
    return module().load_target_evidence_policy(policy_value(), ('000300.SH', '512880.SH'))


def test_v2_catalog_hash_binds_full_document_and_requires_policy(tmp_path):
    value = payload()
    result = load(tmp_path, value)
    expected = sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert result.fingerprint == expected
    assert result.evidence_policy.bootstrap_target_codes == ('000300.SH', '512880.SH')


def test_v2_catalog_loads_bounded_policy_without_changing_targets(tmp_path):
    value = payload()
    value.update(schemaVersion=2, evidencePolicy=policy_value())
    result = load(tmp_path, value)
    p = result.evidence_policy
    assert p.bootstrap_target_codes == ('000300.SH', '512880.SH')
    assert (p.max_http_per_run, p.max_http_per_plan, p.max_retries) == (2, 12, 0)
    assert p.max_response_bytes == 8388608 and not p.collection_enabled
    changed = deepcopy(value)
    changed['evidencePolicy']['maxHttpPerPlan'] = 13
    assert load(tmp_path, changed).fingerprint != result.fingerprint
    assert module().target_evidence_policy_hash(load(tmp_path, changed).evidence_policy) != module().target_evidence_policy_hash(p)


@pytest.mark.parametrize(('key', 'bad'), [
    ('collectionEnabled', 0), ('useScope', 'production'), ('indexMetadataMarket', 'SSE'),
    ('bootstrapTargetCodes', []), ('bootstrapTargetCodes', ['000300.SH', '000300.SH']),
    ('bootstrapTargetCodes', ['999999.SH']), ('bootstrapTargetCodes', '000300.SH'),
    ('maxTargetsPerPlan', 1), ('maxTargetsPerPlan', True), ('maxHttpPerRun', 13),
    ('maxHttpPerRun', True), ('maxHttpPerPlan', 0), ('maxRetries', 1), ('maxRetries', False),
    ('requestsPerMinute', -1), ('lookbackMonths', 1.5), ('maxResponseBytes', '8388608'),
    ('maxResponseRows', 0), ('maxSnapshotMembers', True), ('requestTimeoutSeconds', float('inf')),
    ('requestTimeoutSeconds', float('nan')), ('requestTimeoutSeconds', True),
    ('requestTimeoutSeconds', 0), ('extra', 'unknown'),
])
def test_bad_policy_is_rejected_without_coercion(key, bad):
    value = policy_value()
    value[key] = bad
    with pytest.raises(ValueError):
        module().load_target_evidence_policy(value, ('000300.SH', '512880.SH'))


def test_policy_copies_input_and_hashes_deterministically():
    value = policy_value()
    mod = module()
    p = mod.load_target_evidence_policy(value, ('000300.SH', '512880.SH'))
    value['bootstrapTargetCodes'].append('untrusted')
    assert len(p.bootstrap_target_codes) == 2
    assert mod.target_evidence_policy_hash(p) == mod.target_evidence_policy_hash(policy())


def test_default_catalog_has_explicit_bounded_v2_policy():
    from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
    p = load_enterprise_target_catalog().evidence_policy
    # 本地开关是用户配置，不是离线测试必须保持关闭的前提。
    assert type(p.collection_enabled) is bool and p.max_http_per_run == 2 and p.max_http_per_plan == 12
