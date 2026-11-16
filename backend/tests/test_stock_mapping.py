from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.db.models import Classification, ClassificationMember, DailyBasic, Security, TopHolder
from app.stocks.derived.inputs import CoreSnapshotRepository
from app.stocks.derived.registry import calculate_formula
from app.stocks.marts.stocks import ScreenerMartBuilder, StockWorkbenchMartBuilder
from app.shared.stock_mapping import StockMappingPolicy, DEFAULT_STOCK_MAPPING_FILE
import json

from backend.tests.marts.test_screener_builder import _core_snapshot


def holder_snapshot(new_pg_factory, types=("开放式投资基金",), amounts=("200000",), *, basics=True,
    report_basic=True, report_date=date(2026, 6, 30)):
    factory = new_pg_factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="样本", status="L"))
        session.flush()
        if basics:
            session.add(DailyBasic(ts_code="600519.SH", trade_date=date(2026, 8, 24), data={"total_share": 200, "float_share": 80}))
            if report_basic:
                session.add(DailyBasic(ts_code="600519.SH", trade_date=report_date, data={"total_share": 100, "float_share": 50}))
        for index, (kind, amount) in enumerate(zip(types, amounts)):
            session.add(TopHolder(ts_code="600519.SH", end_date=date(2026, 6, 30),
                ann_date=date(2026, 8, 1), holder_type=kind, holder_name=f"holder-{index}",
                hold_amount=Decimal(amount) if amount is not None else None, data={}))
        # 未来才披露的下一报告期不得进入本次统计。
        session.add(TopHolder(ts_code="600519.SH", end_date=date(2026, 7, 31),
            ann_date=date(2026, 9, 1), holder_type="开放式投资基金", holder_name="future",
            hold_amount=Decimal("900000"), data={}))
    return CoreSnapshotRepository(factory).load(date(2026, 8, 24), ("600519.SH",))


def test_institution_percentage_uses_report_period_total_shares_in_shares_not_latest_float_shares(new_pg_factory):
    snapshot = holder_snapshot(new_pg_factory)
    context = snapshot.security_inputs["600519.SH"]
    metric = calculate_formula("institutionHolding", context.values)
    # 200000股 ÷ (100万股 × 10000) = 20%，不是最新80万流通股或200万总股本。
    assert metric.value == Decimal("20")
    assert metric.quality == "partial_coverage"
    assert context.values["institution_total_shares"] == Decimal("1000000")
    assert "2026-06-30" in context.sources["institution_total_shares"].source_record_key


@pytest.mark.parametrize("kind", ["保险投资组合", "金融机构—证券公司", "金融机构—银行",
    "资产管理公司", "基本养老保险基金", "QFII"])
def test_explicit_financial_holder_types_are_recognized(new_pg_factory, kind):
    result = calculate_formula("institutionHolding", holder_snapshot(new_pg_factory, (kind,)).security_inputs["600519.SH"].values)
    assert result.value == Decimal("20")


@pytest.mark.parametrize("kinds,amounts,want", [
    (("自然人", "一般企业"), ("100", "100"), Decimal("0")),
    (("未知新类型",), ("100",), None),
    ((), (), None),
    (("开放式投资基金",), (None,), None),
    (("开放式投资基金",), ("-1",), None),
    (("开放式投资基金",), ("2000000",), None),
    (("开放式投资基金", "未知新类型"), ("200000", "100"), Decimal("20")),
])
def test_missing_and_invalid_holder_inputs_are_not_normalized_to_zero(new_pg_factory, kinds, amounts, want):
    result = calculate_formula("institutionHolding", holder_snapshot(new_pg_factory, kinds, amounts).security_inputs["600519.SH"].values)
    assert result.value == want


def test_missing_report_period_denominator_does_not_reuse_current_capital(new_pg_factory):
    result = calculate_formula("institutionHolding", holder_snapshot(new_pg_factory, report_basic=False).security_inputs["600519.SH"].values)
    assert result.value is None


def classified_snapshot(*, conflict=False, src="SW2021", future=False):
    base = _core_snapshot()
    nodes = (
        Classification(id=1, provider="tushare", classification_id="l1", name="食品饮料", level=1, data={"src": src}),
        Classification(id=2, provider="tushare", classification_id="l2", name="饮料制造", level=2, parent_id="l1", data={"src": src}),
        Classification(id=3, provider="tushare", classification_id="l3", name="白酒", level=3, parent_id="l2", data={"src": src}),
    )
    members = (ClassificationMember(id=1, classification_id=3, ts_code="600519.SH",
        effective_from=date(2026, 9, 1) if future else date(2026, 1, 1), effective_to=None),)
    if conflict:
        nodes += (Classification(id=4, provider="tushare", classification_id="other", name="其他行业", level=3, parent_id="l2", data={"src": src}),)
        members += (ClassificationMember(id=2, classification_id=4, ts_code="600519.SH", effective_from=date(2026, 1, 1)),)
    return replace(base, classifications=nodes, classification_members=members)


def test_effective_classification_chain_is_shared_by_screener_and_workbench():
    snapshot = classified_snapshot()
    stock = ScreenerMartBuilder().build(snapshot, "v1").payload["stocks"][0]
    quote = StockWorkbenchMartBuilder().build(snapshot, "600519.SH", "v1").payload["quote"]
    assert (stock["majorIndustry"], stock["subIndustry"]) == ("食品饮料", "白酒")
    assert (quote["industry"], quote["subIndustry"]) == ("食品饮料", "白酒")


@pytest.mark.parametrize("kwargs", [{"src": "SW2014"}, {"future": True}])
def test_classification_never_mixes_source_or_future_members(kwargs):
    stock = ScreenerMartBuilder().build(classified_snapshot(**kwargs), "v1").payload["stocks"][0]
    assert (stock["majorIndustry"], stock["subIndustry"]) == ("", "")


@pytest.mark.parametrize("day,want", [(23, Decimal("20")), (22, None)])
def test_report_capital_max_lag_boundary(new_pg_factory, day, want):
    context = holder_snapshot(new_pg_factory, report_date=date(2026, 6, day)).security_inputs["600519.SH"]
    assert calculate_formula("institutionHolding", context.values).value == want


@pytest.mark.parametrize("change", ["overlap", "duplicate", "unknown_field", "negative_lag"])
def test_invalid_mapping_file_is_rejected_not_silently_ignored(change, tmp_path):
    raw = json.loads(DEFAULT_STOCK_MAPPING_FILE.read_text(encoding="utf-8"))
    if change == "overlap":
        raw["institution"]["excludedTypes"].append("机构")
    elif change == "duplicate":
        raw["institution"]["includedTypes"].append("QFII")
    elif change == "unknown_field":
        raw["institution"]["includeTypes"] = []
    else:
        raw["institution"]["maxDenominatorLagDays"] = -1
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        StockMappingPolicy.load(path)


def test_conflicting_classifications_are_missing_not_arbitrarily_selected():
    candidate = ScreenerMartBuilder().build(classified_snapshot(conflict=True), "v1")
    assert candidate.payload["stocks"][0]["subIndustry"] == ""
    assert any(item.get("reason") == "classification_conflict" for item in candidate.missing_data)


def test_classification_nodes_without_members_do_not_invent_membership():
    snapshot = replace(classified_snapshot(), classification_members=())
    candidate = ScreenerMartBuilder().build(snapshot, "v1")
    assert candidate.payload["bootstrap"]["categoryValues"]["majorIndustry"] == []
    assert any(item.get("reason") == "classification_members_missing" for item in candidate.missing_data)


def test_classification_root_zero_is_a_root_not_a_missing_parent():
    snapshot = classified_snapshot()
    snapshot.classifications[0].parent_id = "0"
    stock = ScreenerMartBuilder().build(snapshot, "v1").payload["stocks"][0]
    assert (stock["majorIndustry"], stock["subIndustry"]) == ("食品饮料", "白酒")


def test_institution_deduplicates_latest_visible_revision_not_old_conflicts(pg_factory):
    factory = pg_factory
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="样本", status="L"))
        session.flush()
        session.add(DailyBasic(ts_code="600519.SH", trade_date=date(2026, 6, 30), data={"total_share": 100}))
        for ann, amount in [(1, "100000"), (1, "110000"), (2, "200000")]:
            session.add(TopHolder(ts_code="600519.SH", end_date=date(2026, 6, 30),
                ann_date=date(2026, 8, ann), holder_type="机构", holder_name="same",
                hold_amount=Decimal(amount), data={}))
    context = CoreSnapshotRepository(factory).load(date(2026, 8, 24), ("600519.SH",)).security_inputs["600519.SH"]
    assert calculate_formula("institutionHolding", context.values).value == Decimal("20")


def test_config_change_affects_real_input_assembly_and_lineage(new_pg_factory, monkeypatch, tmp_path):
    import app.shared.stock_mapping as mapping_module
    raw = json.loads(DEFAULT_STOCK_MAPPING_FILE.read_text(encoding="utf-8"))
    raw["institution"]["includedTypes"].append("未知新类型")
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(mapping_module, "DEFAULT_STOCK_MAPPING_FILE", path)
    context = holder_snapshot(new_pg_factory, ("未知新类型",)).security_inputs["600519.SH"]
    assert calculate_formula("institutionHolding", context.values).value == Decimal("20")
    assert StockMappingPolicy.load(path).fingerprint in context.sources["institution_shares"].source_record_key


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_holder_amount_is_missing_not_a_numeric_percentage(pg_factory, value):
    code = "600519.SH"
    period = date(2026, 6, 30)
    security = Security(ts_code=code, symbol=code[:6], name="样本", status="L", data={})
    basic = DailyBasic(ts_code=code, trade_date=period, data={"total_share": 100})
    holder = TopHolder(ts_code=code, end_date=period, ann_date=date(2026, 8, 1),
                       holder_type="开放式投资基金", holder_name="holder", hold_amount=Decimal(value))
    # PostgreSQL 的定精度 numeric 拒绝无穷值，直接检验输入组装器的处理。
    inputs, gaps = CoreSnapshotRepository(pg_factory)._assemble_inputs(
        (security,), (), (basic,), {}, (), (holder,), date(2026, 8, 24), (code,))
    assert any(gap.get("calculationStatus") == "invalid_or_missing_holder_amount" for gap in gaps)
    assert calculate_formula("institutionHolding", inputs[code].values).value is None


def test_classification_conflict_does_not_fall_back_to_security_or_pool_tags():
    snapshot = classified_snapshot(conflict=True)
    snapshot.securities[0].data = {"majorIndustry": "猜测行业", "industry": "另一个行业", "tags": ["科技股"]}
    stock = ScreenerMartBuilder().build(snapshot, "v1").payload["stocks"][0]
    assert (stock["majorIndustry"], stock["subIndustry"]) == ("", "")


@pytest.mark.parametrize("broken_level", [2, 3])
def test_disconnected_members_cannot_be_stitched_into_full_classification(broken_level):
    snapshot = classified_snapshot()
    snapshot.classifications[broken_level - 1].parent_id = None
    snapshot = replace(snapshot, classification_members=snapshot.classification_members + (
        ClassificationMember(id=2, classification_id=1, ts_code="600519.SH", effective_from=date(2026, 1, 1)),))
    result = ScreenerMartBuilder().build(snapshot, "v1")
    assert (result.payload["stocks"][0]["majorIndustry"], result.payload["stocks"][0]["subIndustry"]) == ("", "")
    assert any(item.get("reason") == "classification_invalid_chain" for item in result.missing_data)


@pytest.mark.parametrize("latest_name,want", [("same ", Decimal("20")), ("   ", None)])
def test_holder_name_whitespace_cannot_duplicate_a_revision_or_invent_identity(pg_factory, latest_name, want):
    factory = pg_factory
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="样本", status="L"))
        session.flush()
        session.add(DailyBasic(ts_code="600519.SH", trade_date=date(2026, 6, 30), data={"total_share": 100}))
        for ann, name, amount in [(1, "same", "100000"), (2, latest_name, "200000")]:
            session.add(TopHolder(ts_code="600519.SH", end_date=date(2026, 6, 30),
                ann_date=date(2026, 8, ann), holder_type="机构", holder_name=name,
                hold_amount=Decimal(amount), data={}))
    snapshot = CoreSnapshotRepository(factory).load(date(2026, 8, 24), ("600519.SH",))
    assert calculate_formula("institutionHolding", snapshot.security_inputs["600519.SH"].values).value == want
    if want is None:
        assert any(item.get("calculationStatus") == "invalid_holder_name" for item in snapshot.missing_data)


def test_unusable_finite_capital_is_missing_without_aborting_the_batch(pg_factory):
    factory = pg_factory
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="样本", status="L"))
        session.flush()
        session.add(DailyBasic(ts_code="600519.SH", trade_date=date(2026, 6, 30), data={"total_share": "1e999999"}))
        session.add(TopHolder(ts_code="600519.SH", end_date=date(2026, 6, 30), ann_date=date(2026, 8, 1),
            holder_type="机构", holder_name="known", hold_amount=Decimal("200000"), data={}))
    snapshot = CoreSnapshotRepository(factory).load(date(2026, 8, 24), ("600519.SH",))
    assert calculate_formula("institutionHolding", snapshot.security_inputs["600519.SH"].values).value is None
    assert any(item.get("calculationStatus") == "invalid_report_capital" for item in snapshot.missing_data)


@pytest.mark.parametrize("field", ["provider", "source"])
def test_whitespace_taxonomy_identity_is_rejected(field, tmp_path):
    raw = json.loads(DEFAULT_STOCK_MAPPING_FILE.read_text(encoding="utf-8"))
    raw["classification"][field] = " "
    path = tmp_path / "bad-taxonomy.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        StockMappingPolicy.load(path)
