"""配置拒绝发生在任何数据库连接之前。"""
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import Engine

from app.config import Settings


def assert_fixture_configuration_rejected(monkeypatch):
    with monkeypatch.context() as scoped:
        connect = Mock(side_effect=AssertionError("invalid configuration must not connect"))
        scoped.setattr(Engine, "connect", connect)
        scoped.setenv("DANG_DATA_BACKEND", "fixture")
        with pytest.raises(ValidationError) as error:
            Settings.from_env()
        assert any(item["loc"] == ("data_backend",) and item["type"] == "literal_error"
                   for item in error.value.errors())
        connect.assert_not_called()
