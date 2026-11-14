import json
import pytest


def test_history_policy_defaults_off_for_existing_config_without_section(tmp_path):
    from app.macro.stock_history_policy import load_stock_history_policy
    path = tmp_path / 'financial.json'
    path.write_text('{}', encoding='utf-8')
    assert load_stock_history_policy(path) is False


@pytest.mark.parametrize('enabled', [False, True])
def test_history_policy_uses_existing_config_and_known_contract(tmp_path, enabled):
    from app.macro.stock_history_policy import load_stock_history_policy
    path = tmp_path / 'financial.json'
    path.write_text(json.dumps({'macroHistoryBridge': {'enabled': enabled, 'contractVersion': 'stock-raw-macro-history-v1'}}), encoding='utf-8')
    assert load_stock_history_policy(path) is enabled


@pytest.mark.parametrize('section', [None, True, {'enabled': 1, 'contractVersion': 'stock-raw-macro-history-v1'},
                                   {'enabled': True, 'contractVersion': 'future-v2'},
                                   {'enabled': True, 'contractVersion': 'stock-raw-macro-history-v1', 'force': True}])
def test_history_policy_rejects_unknown_or_unsafe_settings(tmp_path, section):
    from app.macro.stock_history_policy import load_stock_history_policy
    path = tmp_path / 'financial.json'
    path.write_text(json.dumps({'macroHistoryBridge': section}), encoding='utf-8')
    with pytest.raises(ValueError, match='MACRO_HISTORY_POLICY_INVALID'):
        load_stock_history_policy(path)


def test_enterprise_loader_reads_config_gate_without_database_access(monkeypatch):
    from types import SimpleNamespace
    from app.macro.enterprise_contract import enterprise_loader
    from app.macro import stock_history_policy
    calls = []
    monkeypatch.setattr(stock_history_policy, 'load_stock_history_policy', lambda: calls.append('policy') or False)
    settings = SimpleNamespace(macro_enterprise_min_coverage=80, macro_enterprise_min_samples=5,
                               macro_enterprise_max_quarters=40, sync_timezone='Asia/Shanghai')
    assert callable(enterprise_loader(None, settings))
    assert calls == ['policy']
    calls.clear()
    assert callable(enterprise_loader(None, settings, stock_history_enabled=False))
    assert calls == []
