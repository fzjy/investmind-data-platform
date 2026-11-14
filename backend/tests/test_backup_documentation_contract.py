"""保护使用者指定的8.7表格边界及真实归档Git忽略；平台命令验收归Task9。"""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def table(readme: str) -> list[list[str]]:
    section = readme.split("### 8.7 ", 1)[1].split("### 8.8 ", 1)[0]
    return [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in section.splitlines() if line.startswith("|")]


def test_section_87_preserves_task_definitions_while_authorized_results_evolve() -> None:
    baseline = subprocess.run(["git", "show", "4ea13385344219546faffee66a715f154ff9728d:README.md"], cwd=ROOT, capture_output=True, check=True).stdout.decode("utf-8")
    previous, current = table(baseline), table((ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8"))
    assert len(current) == len(previous)
    for before, after in zip(previous, current, strict=True):
        assert after[:4] == before[:4]
        # 使用者已授权完成全部8.7；仅结果列可更新，前四列仍逐项保护。
        assert len(after) == 5


def test_default_backup_packages_are_ignored_by_git() -> None:
    result = subprocess.run(["git", "check-ignore", "backend/backups/20200101T000000Z-" + "a" * 32 + "/database.dump"], cwd=ROOT, capture_output=True)
    assert result.returncode == 0, "backup archives must never be staged accidentally"
