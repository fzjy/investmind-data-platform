from copy import deepcopy
import importlib
import importlib.util

import pytest

from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, catalog_hashes_match


V7 = '2026-09-30-v7'
HASH = '0840df6a99c8b7f197557dce1a68fe3afe5ae1f30c042541dcd24949b3147e9a'


def test_history_binding_entry_exists():
    assert importlib.util.find_spec('app.macro.history_catalog') is not None


@pytest.fixture
def binding():
    if importlib.util.find_spec('app.macro.history_catalog') is None:
        pytest.skip('binding entry not yet implemented')
    return importlib.import_module('app.macro.history_catalog')


@pytest.mark.parametrize('recorded', [HASH,
    '85c55cd9da189b751cafd3736d47095e920c0f8c6a7bac9db3ffa177c4562a31',
    '7c4787cfb46c09b4491c600d28ae27696259c6604e9741626b1367d3500f3fde'])
def test_old_cursor_binds_frozen_contract_without_mutation(binding, recorded):
    current = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    checkpoint = {'catalog_version': V7, 'catalog_hash': recorded, 'unknown': {'old': True}}
    before = deepcopy(checkpoint)
    frozen = binding.bind_history_catalog(current, checkpoint)
    assert frozen.catalog_version == V7 and frozen.catalog_hash == HASH
    assert frozen.require('cn.rate.govbond_10y').sources == ()
    assert checkpoint == before
    assert not catalog_hashes_match(recorded, current.catalog_hash)


def test_new_cursor_binds_current_catalog(binding):
    current = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    assert binding.bind_history_catalog(current, None) is current
    assert binding.bind_history_catalog(current, {'catalog_version': current.catalog_version,
        'catalog_hash': current.catalog_hash}) is current


@pytest.mark.parametrize('checkpoint', [
    {}, [], {'catalog_version': '../../secret', 'catalog_hash': HASH},
    {'catalog_version': V7, 'catalog_hash': 'a' * 64},
    {'catalog_version': '2026-10-07-v8', 'catalog_hash': HASH},
])
def test_unproven_identity_rejected(binding, checkpoint):
    with pytest.raises(ValueError, match='^MACRO_HISTORY_CATALOG_UNAVAILABLE$'):
        binding.bind_history_catalog(MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE), checkpoint)


@pytest.mark.parametrize('change', ['missing', 'content', 'same_version_active'])
def test_archive_and_same_version_content_are_not_trusted(binding, tmp_path, monkeypatch, change):
    current = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    checkpoint = {'catalog_version': V7, 'catalog_hash': HASH}
    if change == 'same_version_active':
        current = current.model_copy(update={'catalog_version': V7})
    else:
        archive = tmp_path / 'archive.json'
        if change == 'content':
            archive.write_bytes(binding.ARCHIVED_V7_FILE.read_bytes() + b'\n')
        monkeypatch.setattr(binding, 'ARCHIVED_V7_FILE', archive)
    with pytest.raises(ValueError, match='^MACRO_HISTORY_CATALOG_UNAVAILABLE$'):
        binding.bind_history_catalog(current, checkpoint)


def test_production_government_bond_source_can_fetch_normal_increment():
    current = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    sources = current.require('cn.rate.govbond_10y').sources
    assert len(sources) == 1
    assert (sources[0].provider, sources[0].api, sources[0].field) == (
        'chinamoney', 'SddsIntrRateGovYldHis', 'tenRate')
    assert sources[0].params == {'maxPages': '24'}


def test_upgrade_changes_only_government_bond_registration(binding):
    current = MacroSeriesCatalog.load(binding.ARCHIVED_V8_FILE)
    old = MacroSeriesCatalog.load(binding.ARCHIVED_V7_FILE)
    assert current.defaults == old.defaults
    for definition in current.series:
        if definition.id != 'cn.rate.govbond_10y':
            assert definition == old.require(definition.id)
        else:
            assert definition.model_dump(exclude={'sources'}) == old.require(definition.id).model_dump(exclude={'sources'})
