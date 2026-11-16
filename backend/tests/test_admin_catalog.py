from pathlib import Path
from app.local_admin.catalog import load_catalog, validate_catalog
from app.main import create_app

ROOT = Path(__file__).resolve().parents[2]


def test_full_catalog_and_inventory():
    catalog = load_catalog(ROOT)
    issues = validate_catalog(catalog, project_root=ROOT, openapi=create_app().openapi())
    assert not issues, issues[:12]
    assert len(catalog.entries) > 40
    assert all(e.fields and e.evidence for e in catalog.entries)
    broken = catalog.model_copy(deep=True)
    broken.ownership.pop(next(iter(broken.ownership)))
    assert any(i["code"] == "MISSING_OWNER" for i in validate_catalog(broken,project_root=ROOT,openapi=create_app().openapi()))


def test_yield_history_and_bilingual_inputs():
    entry = next(e for e in load_catalog(ROOT).entries if e.id == "stock-dividend-yield")
    assert [entry.history[k].value for k in ("records","revisions","query")] == [True,False,False]
    assert len(entry.calculations) == 3
    assert all(i["name"] for c in entry.calculations for i in c["inputs"])
    broken = entry.model_copy(deep=True)
    broken.history["revisions"].value = None
    assert broken.history["revisions"].value is None


def test_daily_prices_distinguish_stored_and_adjusted():
    e = next(e for e in load_catalog(ROOT).entries if e.id=="stock-daily")
    assert "复权" in e.description
    assert any("adjustment_factor" == t.table for t in e.tables)
    assert any(f.role=="response" and f.derived.value is True for f in e.fields)


def test_json_field_paths_and_announcement_coverage():
    catalog=load_catalog(ROOT)
    income=next(e for e in catalog.entries if e.id=="stock-income")
    profit=next(f for f in income.fields if f.id=="n_income_attr_p")
    assert profit.tableRefs[0].fieldPath=="data.n_income_attr_p"
    assert any(e.id=="stock-announcements" for e in catalog.entries)
    broken=catalog.model_copy(deep=True)
    broken.entries[0].tables[0].fieldPath="missing_column"
    assert any(i["code"]=="TABLE_FIELD_MISSING" for i in validate_catalog(broken,project_root=ROOT,openapi=create_app().openapi()))


def test_annual_screener_formula_is_separate_from_historical_ttm():
    entries={e.id:e for e in load_catalog(ROOT).entries}
    assert entries["metric-realMoneyIndex"].access[0]["kind"]=="table"
    current=entries["screener-realMoneyIndex"]
    assert len(current.calculations)==4
    assert all("合计" in c["formula"] for c in current.calculations)
    assert any("Cum3y" in f.id for f in current.fields)
