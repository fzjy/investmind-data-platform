from app.local_admin.control import ControlStore
from app.local_admin.workers import managed_news
import json
from threading import Event, Thread
from types import SimpleNamespace


def test_repeated_enable_does_not_repeat_recovery(tmp_path, control_store):
    state = control_store
    state.target("stocks", True, "19:00")
    one = state.get("stocks")
    execution_id = state.admit("stocks", one["generation"], recovery=True)
    assert execution_id
    assert not state.admit("stocks", one["generation"], recovery=True)
    state.finish("stocks", execution_id, "完成")
    state.target("stocks", True, "18:00")
    assert state.get("stocks")["generation"] == one["generation"]
    state.target("stocks", False, "18:00")
    assert not state.admit("stocks", one["generation"], recovery=False)
    state.target("stocks", True, "18:00")
    assert state.get("stocks")["generation"] > one["generation"]


def test_closed_task_drains_and_preserves_new_target(tmp_path, control_store):
    state = control_store
    state.target("news", True, "")
    gen = state.get("news")["generation"]
    execution_id = state.admit("news", gen, recovery=True)
    assert execution_id
    state.target("news", False, "")
    assert state.get("news")["active"]
    state.target("news", True, "")
    assert not state.admit("news", state.get("news")["generation"], recovery=True)
    state.finish("news", execution_id, "完成")
    execution_id = state.admit("news", state.get("news")["generation"], recovery=True)
    assert execution_id


def test_news_stop_during_collection_finishes_once(tmp_path, control_store):
    path = tmp_path / "news.json"
    path.write_text(json.dumps({"enabled": True, "interval_seconds": 9}))
    store = control_store
    store.target("news", True, "每 9 秒")
    started, release = Event(), Event()
    calls = []
    def runner(settings, *, publish):
        calls.append(publish)
        started.set()
        assert release.wait(5)
        return {"status": "completed"}
    thread = Thread(target=managed_news, args=(store,), kwargs={
        "runner": runner, "settings_loader": lambda: SimpleNamespace(news_config_path=path)})
    thread.start()
    try:
        assert started.wait(3)
        store.target("news", False, "每 9 秒")
        assert store.get("news")["active"]
    finally:
        release.set()
        thread.join(5)
    row = store.get("news")
    assert not thread.is_alive() and calls == [True]
    assert not row["active"] and row["applied"] == row["generation"]


def test_old_schedule_cannot_start_after_rule_changes_back(tmp_path, control_store):
    store = control_store
    store.target("market", True, "19:00")
    before = store.get("market")
    store.target("market", True, "20:00")
    store.target("market", True, "19:00")
    assert store.get("market")["generation"] == before["generation"]
    assert not store.admit("market", before["generation"], recovery=False, revision=before["revision"])


def test_stale_control_heartbeat_means_unknown(tmp_path, control_store):
    from app.local_admin.status import task_rows
    from app.local_admin.configuration import TASKS
    store = control_store
    for task in TASKS:
        store.target(task, task == "stocks", "")
    store.admit("stocks", store.get("stocks")["generation"], recovery=True)
    manager = SimpleNamespace(store=store, workers={"scheduler": SimpleNamespace(poll=lambda: None)}, failed={})
    assert next(r for r in task_rows(manager) if r["name"] == "stocks")["activity"] == "状态待确认"


def test_crash_seen_by_parent_then_cold_start_does_not_replay(tmp_path, control_store):
    store = control_store
    store.target("news", True, "每 60 秒")
    generation = store.get("news")["generation"]
    assert store.admit("news", generation, recovery=True)
    store.reset_instance(["news"])
    store.reset_instance(["news"], cold=True)
    assert store.get("news")["generation"] == generation
    assert not store.admit("news", generation, recovery=True)
    config = tmp_path / "news.json"
    config.write_text('{"interval_seconds":60}')
    def disable_during_wait(_delay):
        store.target("news", False, "每 60 秒")
    def no_replay(*args, **kwargs):
        raise AssertionError("retry incorrectly started another collection")
    managed_news(store, runner=no_replay, sleep=disable_during_wait,
                 settings_loader=lambda:SimpleNamespace(news_config_path=config))

def test_racing_workers_and_owner_takeover(control_store):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import text
    import pytest
    from app.local_admin.control import OwnerLost

    parent = control_store
    parent.target("news", True, "")
    child = ControlStore(parent.factory, parent.owner)
    successor = ControlStore(parent.factory)
    with pytest.raises(OwnerLost):
        successor.acquire()
    barrier = Barrier(2)
    def claim(_):
        barrier.wait()
        return child.admit("news", 1, recovery=True)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, range(2)))
    assert results.count(None) == 1
    old_id = next(value for value in results if value is not None)
    pid = parent.connection.scalar(text("SELECT pg_backend_pid()"))
    with parent.factory.begin() as session:
        assert session.scalar(text("SELECT pg_terminate_backend(:pid, 3000)"), {"pid": pid})
    with pytest.raises(OwnerLost):
        child.admit("news", 1, recovery=False)
    successor.acquire()
    try:
        successor.reset_instance(["news"], cold=True)
        assert successor.admit("news", 1, recovery=True) is None
        current_id = successor.admit("news", 1, recovery=False)
        assert current_id and current_id != old_id
        with pytest.raises(OwnerLost):
            child.finish("news", old_id, "过期")
        successor.finish("news", old_id, "过期")
        assert successor.get("news")["active"]
        successor.finish("news", current_id, "完成")
        assert successor.get("news")["result"] == "完成"
    finally:
        successor.release()
