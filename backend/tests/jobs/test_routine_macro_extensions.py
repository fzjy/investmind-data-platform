"""统一入口仅替换外部组调用；验证真实选择/发布顺序/预算汇总行为。"""
from dataclasses import dataclass

import pytest

from app.config import Settings
from backend.tests.macro.test_extension_plan import config, payload
from backend.tests.macro.test_extension_store import NOW


pytestmark = pytest.mark.usefixtures("isolated_routine_groups")

@dataclass
class Published:
    status: str = 'completed'


def test_extensions_alone_are_selected_and_published_after_execution(monkeypatch, tmp_path):
    from app.jobs import routine_update as module
    raw = payload()
    raw['groups']['usdcnh']['collectionEnabled'] = True
    monkeypatch.setattr(module, 'load_extension_config', lambda: config(tmp_path, raw), raising=False)
    calls = []
    def execute(settings, *, dry_run=False, **kwargs):
        assert ('now' in kwargs) is dry_run, '正式执行不得将整轮开始时刻当检索时钟'
        calls.append('preview' if dry_run else 'collect')
        return {'status': 'preview' if dry_run else 'completed', 'supplierHttp': 0 if dry_run else 1}
    monkeypatch.setattr(module, 'run_macro_extensions', execute, raising=False)
    settings = Settings(data_backend='database', database_url='sqlite://',
        routine_stocks_enabled=False, routine_macro_enabled=False)
    def rebuild(settings):
        calls.append('publish')
        return Published()
    preview = module.run_routine_update(settings, now=NOW, dry_run=True, publish=True, macro_rebuild=rebuild)
    assert preview['status'] == 'planned' and preview['httpCounts']['total'] == 0
    assert calls == ['preview']
    calls.clear()
    result = module.run_routine_update(settings, now=NOW, publish=True, macro_rebuild=rebuild)
    assert result['status'] == 'completed'
    assert result['httpCounts'] == {'stocks': 0, 'macro': 0, 'extensions': 1, 'total': 1}
    assert calls == ['collect', 'publish']


def test_failed_extension_stays_partial_and_keeps_independent_counter(monkeypatch, tmp_path):
    from app.jobs import routine_update as module
    raw = payload()
    raw['groups']['usdcnh']['collectionEnabled'] = True
    monkeypatch.setattr(module, 'load_extension_config', lambda: config(tmp_path, raw), raising=False)
    monkeypatch.setattr(module, 'run_macro_extensions', lambda *args, **kwargs:
        {'status': 'blocked', 'supplierHttp': 1}, raising=False)
    monkeypatch.setattr(module, 'run_macro_catchup', lambda *args, **kwargs:
        {'status': 'completed', 'activeCoverageCompleted': True, 'requestCount': 3})
    result = module.run_routine_update(Settings(data_backend='database', database_url='sqlite://',
        routine_stocks_enabled=False), now=NOW, macro_rebuild=lambda _: Published())
    assert result['status'] == 'partial'
    assert result['httpCounts'] == {'stocks': 0, 'macro': 3, 'extensions': 1, 'total': 4}


def test_zero_http_extension_reentry_does_not_wait_for_previous_zero_http_group(monkeypatch, tmp_path):
    from app.jobs import routine_update as module
    from app.jobs.supplier_job_pacing import SupplierJobPacer
    raw = payload()
    raw['groups']['usdcnh']['collectionEnabled'] = True
    monkeypatch.setattr(module, 'load_extension_config', lambda: config(tmp_path, raw))
    clock, waits = [0.0], []
    def sleep(seconds):
        waits.append(seconds)
        clock[0] += seconds
    pacer = SupplierJobPacer(30, clock=lambda: clock[0], sleep=sleep)
    monkeypatch.setattr(module, 'SupplierJobPacer', lambda _: pacer)
    monkeypatch.setattr(module, 'run_stock_step', lambda *a, **k:
        {'status': 'completed', 'publishReady': True, 'requests': 0})
    monkeypatch.setattr(module, 'run_macro_extensions', lambda *a, **k:
        {'status': 'completed', 'supplierHttp': 0})
    report = module.run_routine_update(Settings(data_backend='database', database_url='sqlite://',
        routine_macro_enabled=False), now=NOW, macro_rebuild=lambda _: Published())
    assert report['httpCounts']['total'] == 0
    assert waits == []


def test_real_new_extension_send_still_paces_after_previous_group(monkeypatch, tmp_path):
    from app.jobs import routine_update as module
    from app.jobs.supplier_job_pacing import SupplierJobPacer
    raw = payload()
    raw['groups']['usdcnh']['collectionEnabled'] = True
    monkeypatch.setattr(module, 'load_extension_config', lambda: config(tmp_path, raw))
    tick, waits = [0.0], []
    def sleep(seconds):
        waits.append(seconds)
        tick[0] += seconds
    monkeypatch.setattr(module, 'SupplierJobPacer', lambda _: SupplierJobPacer(30, clock=lambda: tick[0], sleep=sleep))
    monkeypatch.setattr(module, 'run_stock_step', lambda *a, **k:
        {'status': 'completed', 'publishReady': True, 'requests': 2})
    def send(*a, **kwargs):
        kwargs['before_send']()
        return {'status': 'completed', 'supplierHttp': 1}
    monkeypatch.setattr(module, 'run_macro_extensions', send)
    report = module.run_routine_update(Settings(data_backend='database', database_url='sqlite://',
        routine_macro_enabled=False), now=NOW, macro_rebuild=lambda _: Published())
    assert report['httpCounts']['total'] == 3 and waits == [2.0]
