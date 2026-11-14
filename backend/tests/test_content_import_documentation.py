from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_content_import_and_macro_catchup_have_independent_cli_instructions():
    guide = (ROOT/"backend/README.md").read_text(encoding="utf-8")
    for command in ("plan-macro-catchup", "catchup-macro", "validate-import PATH",
                    "rebuild-external-marts --product content"):
        assert "python -m app.cli.main " + command in guide
    assert "JSON/JSONL" in guide and "版权" in guide
    assert "no_change" in guide and "activeCoverageCompleted" in guide


def test_documented_macro_catchup_budget_is_in_the_shipped_example():
    example = (ROOT/".env.example").read_text(encoding="utf-8")
    guide = (ROOT/"backend/README.md").read_text(encoding="utf-8")
    assert "DANG_MACRO_CATCHUP_REQUEST_LIMIT=100" in example and "DANG_MACRO_CATCHUP_REQUEST_LIMIT=100" in guide
