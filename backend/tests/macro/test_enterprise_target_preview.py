from backend.tests.support.pg_snapshot import database_snapshot
"""防止把未验证库存、ETF代码或固定样本量伪装成可信成分。"""
from datetime import date
from importlib import import_module
import json

import pytest

from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
from app.shared.readonly_database import open_readonly_factory
from app.stocks.universes.models import UniverseSelection
from backend.tests.macro.test_enterprise_cli import database


APIS = ('index_weight', 'etf_basic', 'etf_index', 'etf_sh_cons', 'etf_sz_cons', 'fund_portfolio')


def module():
    try:
        return import_module('app.macro.enterprise_target_preview')
    except ImportError:
        pytest.fail('enterprise target preview is not implemented')


def selection(count):
    return UniverseSelection('custom', 'controlled', 'custom',
        tuple(f'{i:06}.SZ' for i in range(1, count + 1)), 'a' * 64)


@pytest.mark.parametrize('count', [0, 1, 61])
@pytest.mark.parametrize('legacy', [0, 300])
def test_inventory_cannot_prove_membership_even_when_legacy_rows_exist(count, legacy):
    mod = module()
    value = mod.preview_enterprise_targets(selection(count), load_enterprise_target_catalog(),
        mod.TargetEvidenceInventory(legacy, dict.fromkeys(APIS, 20)), date(2024, 8, 1))
    assert value['status'] == 'requirements_pending'
    assert value['pool']['size'] == count and value['pool']['profile'] == 'custom'
    assert value['asOf'] == '2024-08-01' and value['readOnly'] is True
    assert value['membershipReady'] is False and value['accountPermissionProbed'] is False
    assert value['supplierHttp'] == 0 and value['notStrictPointInTime'] is True
    assert value['scope'] == 'configured-pool-intersection'
    assert value['inventory']['evidenceStatus'] == 'unverified_inventory_only'
    assert value['inventory']['legacyConstituentRows'] == legacy
    assert len(value['targets']) == 14
    index, etf = value['targets'][0], value['targets'][3]
    assert index['code'] == '000300.SH' and index['sourceRequirements'] == ['index_basic', 'index_weight']
    assert etf['code'] == '512880.SH' and etf['sourceRequirements'] == ['etf_basic']
    assert etf['sourceIndexNameHint'] == '中证全指证券公司指数'
    assert etf['requiresResolvedIndexMembership'] is True
    for target in value['targets']:
        assert target['verifiedSourceIndexCode'] is None
        assert target['sourceConstituentCount'] is None and target['intersectionCount'] is None
        assert target['status'] == 'missing_verified_membership'
        assert 'coverage' not in target and 'requestParams' not in target
    json.dumps(value, allow_nan=False)


@pytest.mark.parametrize('bad', [-1, True, 1.0, '1', None])
def test_invalid_inventory_counts_are_not_coerced(bad):
    mod = module()
    with pytest.raises(ValueError):
        mod.TargetEvidenceInventory(bad, dict.fromkeys(APIS, 0))
    values = dict.fromkeys(APIS, 0)
    values['index_weight'] = bad
    with pytest.raises(ValueError):
        mod.TargetEvidenceInventory(0, values)


@pytest.mark.parametrize('counts', [{}, {'unknown': 0}])
def test_inventory_requires_exact_known_api_keys(counts):
    with pytest.raises(ValueError):
        module().TargetEvidenceInventory(0, counts)


def test_inventory_copies_mutable_mapping_before_reporting():
    mod = module()
    raw = dict.fromkeys(APIS, 0)
    inventory = mod.TargetEvidenceInventory(2, raw)
    raw['index_weight'] = -1
    value = mod.preview_enterprise_targets(selection(61), load_enterprise_target_catalog(), inventory, date(2024, 8, 1))
    assert value['inventory']['rawApiCounts']['index_weight'] == 0


def test_custom_catalog_not_hardcoded_fourteen_targets(tmp_path):
    from backend.tests.macro.test_enterprise_target_catalog import load, payload
    mod = module()
    value = mod.preview_enterprise_targets(selection(61), load(tmp_path, payload()),
        mod.TargetEvidenceInventory(0, dict.fromkeys(APIS, 0)), date(2024, 8, 1))
    assert len(value['targets']) == 2 and value['catalogVersion'] == 'controlled-v2'


def test_actual_readonly_repository_counts_inventory_without_reading_raw_body(database):
    mod = module()
    repository = import_module('app.macro.enterprise_target_repository')
    from app.db.models import IndexConstituent, IngestionBatch, SourcePayload
    db_engine, factory = database
    with factory.begin() as session:
        session.add(IngestionBatch(id='controlled-source', source='tushare', dataset='index', status='success'))
        session.add(IndexConstituent(index_code='000300.SH', ts_code='000001.SZ', effective_from=date(2024, 8, 1), weight=1))
        for i, api in enumerate(['index_weight', 'index_weight', 'etf_basic', 'unrelated']):
            session.add(SourcePayload(batch_id='controlled-source', api_name=api, request_hash=f'{i:064}',
                content_hash='b' * 64, payload={'private': 'not-output'}))
    before = database_snapshot(db_engine)
    with open_readonly_factory(db_engine.url.render_as_string(hide_password=False)) as readonly:
        inventory = repository.load_target_evidence_inventory(readonly)
        result = mod.preview_enterprise_targets(selection(61), load_enterprise_target_catalog(), inventory, date(2024, 8, 1))
    assert result['inventory']['legacyConstituentRows'] == 1
    assert result['inventory']['rawApiCounts'] == {
        'index_weight': 2, 'etf_basic': 1, 'etf_index': 0, 'etf_sh_cons': 0, 'etf_sz_cons': 0, 'fund_portfolio': 0,
    }
    assert result['membershipReady'] is False
    assert 'not-output' not in json.dumps(result)
    assert database_snapshot(db_engine) == before
