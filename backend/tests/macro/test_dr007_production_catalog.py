from copy import deepcopy
import pytest

from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro import history_catalog as binding

V8='2026-10-07-v8'
H8='5779a4153377655b54f6e2206ad01bcc909d4788421784a8a9016ffc82567713'


def test_v9_source_is_exact_dr007_and_other_contracts_unchanged():
    current=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    assert current.catalog_version=='2026-10-08-v9'
    old=MacroSeriesCatalog.load(binding.ARCHIVED_V8_FILE)
    assert old.catalog_version==V8 and old.catalog_hash==H8
    assert current.defaults==old.defaults
    for definition in current.series:
        if definition.id!='cn.rate.dr007':
            assert definition==old.require(definition.id)
        else:
            assert definition.model_dump(exclude={'sources'})==old.require(definition.id).model_dump(exclude={'sources'})
            source=definition.sources[0]
            assert len(definition.sources)==1
            assert (source.provider,source.api,source.series,source.field)==('tushare','repo_daily','DR007.IB','weight')
            assert source.params=={'ts_code':'DR007.IB','maxPages':'24'}


def test_fixed_v8_binding_does_not_rewrite_checkpoint():
    current=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    future=current.model_copy(update={'catalog_version':'2026-10-08-v9'})
    checkpoint={'catalog_version':V8,'catalog_hash':H8,'keep':{'old':True}}
    before=deepcopy(checkpoint)
    frozen=binding.bind_history_catalog(future,checkpoint)
    assert frozen.catalog_version==V8 and frozen.catalog_hash==H8
    assert frozen.require('cn.rate.dr007').sources==()
    assert checkpoint==before


@pytest.mark.parametrize('kind',['missing','changed'])
def test_v8_archive_missing_or_changed_is_not_accepted(tmp_path,monkeypatch,kind):
    assert hasattr(binding,'ARCHIVED_V8_FILE')
    current=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).model_copy(update={'catalog_version':'2026-10-08-v9'})
    file=tmp_path/'v8.json'
    if kind=='changed':
        file.write_bytes(binding.ARCHIVED_V8_FILE.read_bytes()+b'\n')
    monkeypatch.setattr(binding,'ARCHIVED_V8_FILE',file)
    with pytest.raises(ValueError,match='^MACRO_HISTORY_CATALOG_UNAVAILABLE$'):
        binding.bind_history_catalog(current,{'catalog_version':V8,'catalog_hash':H8})
