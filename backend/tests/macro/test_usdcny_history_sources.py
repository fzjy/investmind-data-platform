import json
from pathlib import Path

import pytest

from app.macro.jobs.dr007_history import load_dr007_source
from app.macro.jobs.govbond_history import DEFAULT_HISTORY_SOURCES_FILE, load_govbond_source
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSourceDefinition
from backend.tests.connectors.macro.test_chinamoney_usdcny import _source


@pytest.mark.parametrize('loader', [load_govbond_source, load_dr007_source])
def test_existing_sources_load_with_known_usdcny_extension_without_identity_change(tmp_path, loader):
    before = loader()
    manifest = json.loads(DEFAULT_HISTORY_SOURCES_FILE.read_text(encoding='utf-8'))
    manifest['sources']['cn.fx.usdcny'] = _source().model_dump(mode='json', by_alias=True)
    path = tmp_path / 'macro-history-sources.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    (tmp_path / 'macro-series.json').write_bytes(DEFAULT_MACRO_CATALOG_FILE.read_bytes())
    try:
        after = loader(path)
    except ValueError as exc:
        pytest.fail(f'合法USD/CNY扩展破坏旧来源加载：{exc}')
    assert after == before


@pytest.mark.parametrize('loader', [load_govbond_source, load_dr007_source])
def test_source_extension_does_not_allow_unknown_keys(tmp_path, loader):
    manifest = json.loads(DEFAULT_HISTORY_SOURCES_FILE.read_text(encoding='utf-8'))
    manifest['sources']['cn.fx.unknown'] = _source().model_dump(mode='json', by_alias=True)
    path = tmp_path / 'sources.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    (tmp_path / 'macro-series.json').write_bytes(DEFAULT_MACRO_CATALOG_FILE.read_bytes())
    with pytest.raises(ValueError, match='HISTORY_SOURCE_INVALID'):
        loader(path)


def test_history_manifest_has_usdcny_but_production_catalog_does_not():
    manifest = json.loads(DEFAULT_HISTORY_SOURCES_FILE.read_text(encoding='utf-8'))
    assert 'cn.fx.usdcny' in manifest['sources'], '缺USD/CNY历史专用来源'
    source = MacroSourceDefinition.model_validate(manifest['sources']['cn.fx.usdcny'])
    assert source == _source()
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    assert catalog.catalog_version == '2026-10-08-v9'
    assert catalog.require('cn.fx.usdcny').sources == ()


@pytest.mark.parametrize('mutation', ['unknown', 'url', 'budget', 'missing'])
def test_usdcny_source_loader_rejects_corrupt_manifest(tmp_path, mutation):
    from app.macro.jobs.usdcny_history import load_usdcny_source
    manifest = json.loads(DEFAULT_HISTORY_SOURCES_FILE.read_text(encoding='utf-8'))
    manifest['sources']['cn.fx.usdcny'] = _source().model_dump(mode='json', by_alias=True)
    if mutation == 'unknown':
        manifest['sources']['unknown'] = {}
    elif mutation == 'url':
        manifest['sources']['cn.fx.usdcny']['sourceUrl'] += '?token=source-secret-canary'
    elif mutation == 'budget':
        manifest['sources']['cn.fx.usdcny']['params'] = {'maxPages': '3'}
    else:
        del manifest['sources']['cn.fx.usdcny']
    path = tmp_path / 'macro-history-sources.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='^USDCNY_HISTORY_SOURCE_INVALID$'):
        load_usdcny_source(path)
