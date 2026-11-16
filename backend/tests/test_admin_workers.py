from app.local_admin.control import ControlStore


def test_repeated_enable_does_not_repeat_recovery(tmp_path):
    state = ControlStore(tmp_path / "control.sqlite3")
    state.target("stocks", True, "19:00")
    one = state.get("stocks")
    assert state.admit("stocks", one["generation"], recovery=True)
    assert not state.admit("stocks", one["generation"], recovery=True)
    state.finish("stocks", "完成")
    state.target("stocks", True, "18:00")
    assert state.get("stocks")["generation"] == one["generation"]
    state.target("stocks", False, "18:00")
    assert not state.admit("stocks", one["generation"], recovery=False)
    state.target("stocks", True, "18:00")
    assert state.get("stocks")["generation"] > one["generation"]


def test_closed_task_drains_and_preserves_new_target(tmp_path):
    state = ControlStore(tmp_path / "db")
    state.target("news", True, "")
    gen = state.get("news")["generation"]
    assert state.admit("news", gen, recovery=True)
    state.target("news", False, "")
    assert state.get("news")["active"]
    state.target("news", True, "")
    assert not state.admit("news", state.get("news")["generation"], recovery=True)
    state.finish("news", "完成")
    assert state.admit("news", state.get("news")["generation"], recovery=True)
