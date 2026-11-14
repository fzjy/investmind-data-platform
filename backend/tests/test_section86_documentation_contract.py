from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_section86_commands_follow_platform_and_shared_configuration():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    mac = readme.split("## 4. 如何运行项目-mac系统", 1)[1].split("## 5.", 1)[0]
    windows = readme.split("## 5. 如何运行项目-windows系统", 1)[1].split("## 6.", 1)[0]
    backend = (ROOT / "backend/README.md").read_text(encoding="utf-8")
    for chapter, npm in ((mac, "npm"), (windows, "npm.cmd")):
        assert "(backend/README.md)" in chapter
        for command in ("plan-macro-catchup", "catchup-macro", "rebuild-external-marts --product content"):
            assert f"{npm} run backend:cli -- {command}" in backend
        assert f"{npm} run backend:update -- --dry-run" in chapter
        assert f"{npm} run backend:update -- --publish" in chapter
    assert "DANG_MACRO_CATCHUP_REQUEST_LIMIT" in backend
    assert "no_change" in backend and "activeCoverageCompleted" in backend


def test_section86_table_retains_definitions_and_excludes_collectors():
    readme = (ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")
    chapter = readme.split("### 8.6 ", 1)[1].split("### 8.7", 1)[0]
    rows = [line.split("|")[1:-1] for line in chapter.splitlines() if line.startswith("| ")]
    matters = {row[0].strip(): row[-1] for row in rows[2:]}
    assert len(matters) == 8
    for key in ("建立证券关联", "建立版权与正文策略", "接通导入后的 Mart 重建"):
        assert "已完成" in matters[key]
    assert "已实现待审核" in matters["编写新闻采集管道"]
    assert "未完成" in matters["编写公告采集管道"]
    assert "docs/data-sources.md" in chapter
    assert "18" in chapter and "暂停" in chapter


def test_source_registry_and_default_budget_are_not_new_runtime_files():
    registry = (ROOT / "docs/data-sources.md").read_text(encoding="utf-8")
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DANG_MACRO_CATCHUP_REQUEST_LIMIT=100" in example
    for required in ("Tushare", "NBS", "FRED", "BLS", "版权", "故障", "授权", "JSON/JSONL"):
        assert required in registry
    assert "不是额外运行配置" in registry
