"""测试入口隔离用户采集开关；不改写真实配置。"""
import json

from backend.tests.macro.test_extension_plan import payload


def test_isolated_config_disables_live_switches_but_preserves_explicit_config(tmp_path, monkeypatch):
    from app.macro.extensions import config
    from backend.tests import run_isolated
    raw = payload()
    for group in raw['groups'].values():
        group['collectionEnabled'] = True
    source = tmp_path / 'user-config.json'
    source.write_text(json.dumps(raw), encoding='utf-8')
    original = source.read_bytes()
    monkeypatch.setattr(config, 'DEFAULT_EXTENSION_CONFIG_FILE', source)
    private = tmp_path / 'isolated'
    private.mkdir()
    run_isolated.isolate_extension_config(private)
    assert all(not group.collectionEnabled for group in config.load_extension_config().groups.values())
    assert all(group.collectionEnabled for group in config.load_extension_config(source).groups.values())
    assert source.read_bytes() == original
