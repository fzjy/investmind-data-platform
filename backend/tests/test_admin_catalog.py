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


def test_market_and_reference_inputs_explain_sources_and_ownership():
    entries={e.id:e for e in load_catalog(ROOT).entries}
    market=entries["market-overview"]
    assert market.sources
    assert {"daily_price","daily_basic","security"} <= {t.table for t in market.tables}
    assert market.derived.value is True
    assert any(c["id"]=="threshold-v1" for c in market.calculations)
    public=entries["portfolio-inputs"]
    assert public.name=="资产价格、分红与基准公共输入"
    assert "持仓" in public.description and "消费项目" in public.description
    assert {"daily_price","dividend","macro_observation"} <= {t.table for t in public.tables}
    assert public.sources


def test_catalog_provenance_and_derivation_are_consistent():
    catalog=load_catalog(ROOT)
    for entry in catalog.entries:
        assert entry.sources, entry.id
        assert any(t.schema_ in {"core","raw"} for t in entry.tables), entry.id
        if entry.derived.value is True:
            assert entry.calculations, entry.id
        if any(f.derived.value is True for f in entry.fields):
            assert entry.derived.value is True, entry.id
    broken=catalog.model_copy(deep=True)
    market=next(e for e in broken.entries if e.id=="market-overview")
    market.sources=[]
    market.calculations=[]
    market.tables=[t for t in market.tables if t.schema_=="mart"]
    codes={i["code"] for i in validate_catalog(broken,project_root=ROOT,openapi=create_app().openapi()) if i["entryId"]==market.id}
    assert {"SOURCE_MISSING","INPUT_TABLE_MISSING","CALCULATION_MISSING"} <= codes


def test_specific_inputs_placeholders_and_contextual_field_labels():
    entries={e.id:e for e in load_catalog(ROOT).entries}
    assert any(t.table=="daily_basic" for t in entries["screener-pePercentile"].tables)
    assert any(t.table=="macro_observation" for t in entries["macro-extension-vix"].tables)
    assert entries["macro-extension-nominalGdp"].derived.value is True
    assert entries["stock-research"].derived.value is True
    assert entries["screener-longMoneyHoldingRatio"].derived.value is None
    assert "指数" in entries["macro-enterprise"].name
    assert "政策" in entries["macro-enterprise"].description
    assert next(f for f in entries["stock-unlock"].fields if f.id=="float_share").name=="解禁股数"
    assert next(f for f in entries["stock-repurchase"].fields if f.id=="exp_date").name=="回购失效日期"
    for entry_id, field_id in [("portfolio-inputs","data.prices"),("market-overview","data.sentimentSnapshot"),("market-explorer","data.heatmapData")]:
        f=next(f for f in entries[entry_id].fields if f.id==field_id)
        assert f.derived.value is True and f.tableRefs


def test_enterprise_base_card_direction_is_separate_from_consumer_projection():
    entry=next(e for e in load_catalog(ROOT).entries if e.id=="macro-enterprise")
    formula=next(c["formula"] for c in entry.calculations if c["id"]=="enterprise-cards")
    assert "cards.scissors" in formula and "营收同比 − 归母净利润同比" in formula
    assert "cards.dividend" in formula and "资本开支金额 − 分红偿息支付金额" in formula
    assert "rea.series.dividend" in formula and "亿元" in formula
    assert any(t.table=="trade_calendar" for t in entry.tables)
    assert "首个完整交易日" in formula and "valuation-calendar-missing" in entry.calculations[0]["missing"]
