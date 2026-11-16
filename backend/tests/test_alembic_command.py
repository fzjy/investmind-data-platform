from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def test_alembic_config_resolves_paths_from_project_root() -> None:
    project_root = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(project_root / "backend" / "alembic.ini"),
            "heads",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "20261116_0022 (head)"


def test_documented_python_migration_commands_use_the_packaged_configuration() -> None:
    root = Path(__file__).resolve().parents[2]
    guide = (root/"docs/local-operations.md").read_text(encoding="utf-8")
    for command in ("current", "upgrade head"):
        assert "python -m alembic -c backend/alembic.ini " + command in guide
    assert "python -m app.cli.main" in guide
    assert (root/"backend/migrations/env.py").is_file()
