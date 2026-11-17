"""只读快照须识别仅涉及数据库保护规则的变更。"""
import pytest
from sqlalchemy import inspect

from backend.tests.support.pg_snapshot import database_snapshot


@pytest.mark.parametrize("change", ["disable-trigger", "drop-check"])
def test_database_snapshot_detects_protection_changes(pg_factory, change):
    engine = pg_factory.kw["bind"]
    before = database_snapshot(engine)
    with engine.begin() as connection:
        if change == "disable-trigger":
            connection.exec_driver_sql(
                "ALTER TABLE raw.enterprise_target_observation DISABLE TRIGGER immutable_enterprise_target"
            )
        else:
            checks = inspect(connection).get_check_constraints("enterprise_target_execution_plan", schema="ops")
            slot_check, = [item["name"] for item in checks if "slot" in item["sqltext"]]
            name = connection.dialect.identifier_preparer.quote(slot_check)
            connection.exec_driver_sql(f"ALTER TABLE ops.enterprise_target_execution_plan DROP CONSTRAINT {name}")
    assert database_snapshot(engine) != before
