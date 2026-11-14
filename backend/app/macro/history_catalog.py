"""历史队列绑定已记录的不可变契约，不将真实目录升级当hash兼容。"""
from collections.abc import Mapping

from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, catalog_hashes_match


ARCHIVED_V7_FILE = DEFAULT_MACRO_CATALOG_FILE.with_name('macro-series-v7.json')
V7_VERSION = '2026-09-30-v7'
V7_HASH = '0840df6a99c8b7f197557dce1a68fe3afe5ae1f30c042541dcd24949b3147e9a'
ARCHIVED_V8_FILE = DEFAULT_MACRO_CATALOG_FILE.with_name('macro-series-v8.json')
V8_VERSION = '2026-10-07-v8'
V8_HASH = '5779a4153377655b54f6e2206ad01bcc909d4788421784a8a9016ffc82567713'


def bind_history_catalog(active: MacroSeriesCatalog, checkpoint: object | None) -> MacroSeriesCatalog:
    if checkpoint is None:
        return active
    if not isinstance(checkpoint, Mapping):
        raise ValueError('MACRO_HISTORY_CATALOG_UNAVAILABLE')
    version, recorded = checkpoint.get('catalog_version'), checkpoint.get('catalog_hash')
    if version == active.catalog_version:
        if catalog_hashes_match(recorded, active.catalog_hash):
            return active
        raise ValueError('MACRO_HISTORY_CATALOG_UNAVAILABLE')
    archives = {V7_VERSION: (ARCHIVED_V7_FILE,V7_HASH), V8_VERSION: (ARCHIVED_V8_FILE,V8_HASH)}
    if version not in archives:
        raise ValueError('MACRO_HISTORY_CATALOG_UNAVAILABLE')
    path,expected = archives[version]
    if not catalog_hashes_match(recorded,expected):
        raise ValueError('MACRO_HISTORY_CATALOG_UNAVAILABLE')
    try:
        frozen = MacroSeriesCatalog.load(path)
    except ValueError:
        raise ValueError('MACRO_HISTORY_CATALOG_UNAVAILABLE') from None
    if frozen.catalog_version != version or frozen.catalog_hash != expected:
        raise ValueError('MACRO_HISTORY_CATALOG_UNAVAILABLE')
    return frozen


def inherited_gap_contract(catalog_version: str, catalog_hash: str
                           ) -> tuple[MacroSeriesCatalog, frozenset[str]] | None:
    candidates = inherited_gap_contracts(catalog_version,catalog_hash)
    return candidates[0] if candidates else None


def inherited_gap_contracts(catalog_version: str, catalog_hash: str
                            ) -> tuple[tuple[MacroSeriesCatalog,frozenset[str]], ...]:
    """已知前驱按新到旧选显式缺口；逐指标证明合同，不继承完成状态。"""
    predecessors = {V8_VERSION: ((V7_VERSION,V7_HASH),),
        '2026-10-08-v9': ((V8_VERSION,V8_HASH),(V7_VERSION,V7_HASH))}
    if catalog_version not in predecessors:
        return ()
    active = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    if active.catalog_version != catalog_version or active.catalog_hash != catalog_hash:
        return ()
    candidates = []
    for version,recorded in predecessors[catalog_version]:
        frozen = bind_history_catalog(active, {'catalog_version':version,'catalog_hash':recorded})
        if active.defaults != frozen.defaults:
            continue
        old = {series.id:series for series in frozen.series}
        unchanged = frozenset(series.id for series in active.series
            if series.id in old and series.model_dump()==old[series.id].model_dump())
        candidates.append((frozen,unchanged))
    return tuple(candidates)
