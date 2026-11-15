import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("start_project", ROOT / "scripts/start-project.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_invalid_task_value_fails_before_launch(tmp_path):
    tasks = launcher.load_tasks(launcher.CONFIG)
    tasks["news"] = "false"
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(tasks))
    with pytest.raises(ValueError, match="news"):
        launcher.load_tasks(path)


@pytest.mark.parametrize(("active", "started", "stopped"), [
    ([], ["api"], ["news-worker", "scheduler"]),
    (["news"], ["api", "news-worker"], ["scheduler"]),
    (["regions"], ["api", "scheduler"], ["news-worker"]),
    (["stocks", "news"], ["api", "news-worker", "scheduler"], []),
])
def test_compose_reconciles_selected_workers(monkeypatch, active, started, stopped):
    tasks = {name: name in active for name in launcher.load_tasks(launcher.CONFIG)}
    calls = []

    def record(command, **kwargs):
        for key, env in launcher.SCHEDULER_ENV.items():
            assert kwargs["env"][env] == str(key in active).lower()
        calls.append(command[command.index("--profile") + 2:])
        return subprocess.CompletedProcess(command, 0, stdout="existing-api")

    monkeypatch.setattr(launcher.subprocess, "run", record)
    launcher.start_compose(tasks, [])
    assert calls[-1] == ["up", "-d", "--no-deps", *started]
    assert [cmd[1:] for cmd in calls if cmd[0] == "stop"] == ([stopped] if stopped else [])
