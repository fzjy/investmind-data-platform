"""日常任务测试显式选择需要的组，使开发机当前采集配置不影响测试。"""
from types import SimpleNamespace

import pytest


@pytest.fixture
def isolated_routine_groups(monkeypatch):
    from app.jobs import routine_update

    for policy in (
        "portfolio_routine_config", "financial_supply_config", "business_supply_config",
        "valuation_supply_config", "announcement_policy",
    ):
        monkeypatch.setattr(routine_update, policy, lambda: {"enabled": False})
    monkeypatch.setattr(routine_update, "load_extension_config", lambda: SimpleNamespace(groups={}))
