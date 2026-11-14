from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import importlib.util

from backend.tests.ops.backup.test_files import manifest


def module():
    assert importlib.util.find_spec("app.ops.backup.retention") is not None, "same-source retention is missing"
    return importlib.import_module("app.ops.backup.retention")


def packages(count: int = 10, identity: str = "a" * 64):
    return tuple(manifest("20200101T000000Z-" + f"{index:032x}").model_copy(update={"completed_at": datetime(2020, 1, 2, tzinfo=timezone.utc) + timedelta(days=index), "source_identity": identity}) for index in range(count))


def test_count_and_age_both_required() -> None:
    planner = module().plan_retention
    values = packages()
    verified = frozenset(value.backup_id for value in values[-2:])
    assert planner(values[:8], verified_ids=verified, now=datetime(2021, 1, 1, tzinfo=timezone.utc), keep_count=8, retention_days=56) == ()
    assert planner(values, verified_ids=verified, now=datetime(2020, 2, 27, tzinfo=timezone.utc), keep_count=8, retention_days=56) == ()
    assert planner(values, verified_ids=verified, now=datetime(2020, 2, 28, tzinfo=timezone.utc), keep_count=8, retention_days=56) == (values[0].backup_id,)
    assert planner(values, verified_ids=verified, now=datetime(2021, 1, 1, tzinfo=timezone.utc), keep_count=8, retention_days=56) == (values[0].backup_id, values[1].backup_id)


def test_two_verified_protected_and_other_source_cannot_fill_gap() -> None:
    planner = module().plan_retention
    values = packages()
    other = tuple(value.model_copy(update={"backup_id": "20200101T000000Z-" + f"{index + 20:032x}", "source_identity": "b" * 64}) for index, value in enumerate(packages(2)))
    verified = frozenset([values[-1].backup_id, *(value.backup_id for value in other)])
    assert planner(values + other, verified_ids=verified, now=datetime(2021, 1, 1, tzinfo=timezone.utc), keep_count=8, retention_days=56) == ()
    verified = frozenset([values[0].backup_id, values[-1].backup_id])
    assert planner(values, verified_ids=verified, now=datetime(2021, 1, 1, tzinfo=timezone.utc), keep_count=8, retention_days=56) == (values[1].backup_id,)


def test_latest_verified_is_protected_even_when_very_old() -> None:
    planner = module().plan_retention
    values = packages()
    verified = frozenset(value.backup_id for value in values[:3])
    assert planner(values, verified_ids=verified, now=datetime(2021, 1, 1, tzinfo=timezone.utc), keep_count=2, retention_days=56) == (values[0].backup_id, *(value.backup_id for value in values[3:8]))
