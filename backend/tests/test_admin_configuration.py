import json
from pathlib import Path
from shutil import copytree
import pytest
from app.local_admin.configuration import Configuration, Conflict


@pytest.fixture
def cfg(tmp_path):
    copytree(Path(__file__).resolve().parents[2] / "config", tmp_path / "config")
    return Configuration(tmp_path)


def test_stale_form_keeps_external_changes(cfg):
    original = cfg.read("tasks")
    cfg.save("tasks", original["version"], {"news": True})
    with pytest.raises(Conflict):
        cfg.save("tasks", original["version"], {"macro": True})
    assert cfg.read("tasks")["fields"]["news"]


@pytest.mark.parametrize("file,change", [("tasks", {"news": 1}), ("news", {"interval_seconds": 0}),
                                          ("news", {"secret": "x"}), ("market", {"time": "28:45"})])
def test_invalid_patch_keeps_original_bytes(cfg, file, change):
    original = cfg.read(file)
    with pytest.raises(ValueError):
        cfg.save(file, original["version"], change)
    assert cfg.read(file) == original
