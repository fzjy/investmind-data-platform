from __future__ import annotations

import json
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
    assert result.stdout.strip() == "20261111_0020 (head)"


def test_package_exposes_cross_platform_database_commands() -> None:
    project_root = Path(__file__).resolve().parents[2]
    package = json.loads((project_root / "package.json").read_text(encoding="utf-8"))

    assert package["scripts"]["db:migrate"] == (
        "node scripts/run-backend-python.mjs alembic "
        "-c backend/alembic.ini upgrade head"
    )
    assert package["scripts"]["db:current"] == (
        "node scripts/run-backend-python.mjs alembic "
        "-c backend/alembic.ini current"
    )
    assert package["scripts"]["backend:cli"] == (
        "node scripts/run-backend-python.mjs app.cli.main"
    )
