from dataclasses import replace
from datetime import date
from decimal import Decimal
import pytest

from app.db.models import (
    Classification,
    ClassificationMember,
    DailyBasic,
    DailyPrice,
    Security,
)
from app.market.marts.market import MarketMartBuilder
from app.marts.dto import validate_product_payload
from backend.tests.marts.test_screener_builder import _core_snapshot


def _market_snapshot():
    base = _core_snapshot()
    second = Security(
        ts_code="000001.SZ",
        symbol="000001",
        name="平安银行",
        exchange="SZSE",
        status="L",
    )
    return replace(
        base,
        securities=base.securities + (second,),
        daily_prices=base.daily_prices
        + (
            DailyPrice(
                ts_code="000001.SZ",
                trade_date=date(2026, 8, 24),
                close=Decimal("12"),
                pct_chg=Decimal("-1"),
                amount=Decimal("2000"),
            ),
        ),
        daily_basics=base.daily_basics
        + (
            DailyBasic(
                ts_code="000001.SZ",
                trade_date=date(2026, 8, 24),
                data={"limit_status": 5},
            ),
        ),
    )


def test_tagged_status_requires_verified_mapping_not_unchecked_json():
    snapshot = _market_snapshot()
    for basic in snapshot.daily_basics:
        basic.data = {'limit_status': 2, '_limitStatusLineage': {'sourcePayloadId': 999}}
    overview, _ = MarketMartBuilder().build(snapshot, 'v1')
    assert overview.payload['limitStats']['coverage']['valid'] == 0


def test_market_builder_aggregates_latest_day_and_reports_missing_sources() -> None:
    overview, explorer = MarketMartBuilder().build(_market_snapshot(), "v1")

    assert overview.payload["turnoverSnapshot"]["value"] == "3000"
    assert set(overview.payload["marketSnapshot"]) == {
        "asOf", "delayLabel", "aggregationLabel", "support",
    }
    assert set(overview.payload["turnoverSnapshot"]) == {
        "value", "unit", "title", "explanation",
    }
    assert set(overview.payload["limitStats"]) == {
        "riseLimit", "fallLimit", "status", "explanation", "coverage",
    }
    assert set(overview.payload["sentimentSnapshot"]) == {
        "algorithm", "tradeDate", "score", "level", "levelBasis", "formula", "status", "reason", "policy", "counts", "coverage", "riseRatio", "thresholdRiseRatio", "thresholdRetreatRatio", "contributions",
    }
    assert overview.payload["limitStats"]["fallLimit"] == 1
    assert explorer.payload["rankingLeaders"][0]["symbol"] == "600519.SH"
    missing = {item["source"] for item in overview.payload["missingData"]}
    assert {"indices", "etfs", "fundFlow"} <= missing
    malformed = dict(explorer.payload)
    malformed["heatmapCategories"] = [{"id": "majorIndustry", "name": "行业"}]
    malformed["heatmapData"] = {
        "majorIndustry": [{"symbol": "600519.SH", "weight": 1}]
    }
    errors = validate_product_payload("market-explorer", malformed)
    assert "heatmapCategories[0].label" in errors
    assert "heatmapData.majorIndustry[0].stocks" in errors


def test_market_builder_maps_classification_to_heatmap_contract() -> None:
    classification = Classification(
        id=1,
        provider="tushare",
        source="SW2021",
        classification_id="food",
        name="食品饮料",
        level=1,
        data={},
    )
    member = ClassificationMember(
        classification_id=1,
        ts_code="600519.SH",
        effective_from=date(2020, 1, 1),
        weight=Decimal("1"),
    )
    snapshot = replace(
        _market_snapshot(),
        classifications=(classification,),
        classification_members=(member,),
    )

    _overview, explorer = MarketMartBuilder().build(snapshot, "v1")

    assert explorer.payload["heatmapCategories"] == [
        {"id": "majorIndustry", "label": "大类行业", "total": 1},
        {"id": "region", "label": "地域", "total": 1},
    ]
    sector = explorer.payload["heatmapData"]["majorIndustry"][0]
    assert set(sector) == {
        "id", "name", "change", "marketCap", "stockCount", "stocks", "turnover", "coverage",
    }
    assert sector["stocks"][0]["symbol"] == "600519.SH"
    assert validate_product_payload("market-explorer", explorer.payload) == ()


def test_market_heatmap_converts_provider_amounts_to_yi_yuan_once() -> None:
    snapshot = _market_snapshot()
    snapshot.daily_basics[0].total_mv = Decimal("1000000")
    snapshot.daily_prices[0].amount = Decimal("100000")
    snapshot = replace(snapshot, classifications=(Classification(id=1, provider="tushare", source="SW2021", classification_id="food", name="食品", level=1, data={}),), classification_members=(ClassificationMember(classification_id=1, ts_code="600519.SH", effective_from=date(2020, 1, 1)),))
    _, explorer = MarketMartBuilder().build(snapshot, "v1")
    sector = explorer.payload["heatmapData"]["majorIndustry"][0]
    assert sector["marketCap"] == 100
    assert sector["turnover"] == 1
    assert sector["stocks"][0]["marketCap"] == 100
    assert sector["stocks"][0]["turnover"] == 1
    assert explorer.payload["amountUnit"] == "yi-yuan"


def test_missing_limit_and_change_coverage_is_not_zero() -> None:
    snapshot = _market_snapshot()
    for row in snapshot.daily_basics:
        row.data = {}
    for row in snapshot.daily_prices:
        row.pct_chg = None
    overview, _ = MarketMartBuilder().build(snapshot, "v1")
    assert overview.payload["limitStats"]["riseLimit"] is None
    assert overview.payload["limitStats"]["fallLimit"] is None
    assert overview.payload["limitStats"]["coverage"] == {"valid": 0, "total": 2}
    assert overview.payload["sentimentSnapshot"]["score"] is None
    assert validate_product_payload("market-overview", overview.payload) == ()


@pytest.mark.parametrize("invalid", [True, 7, -1, "2.5", 2.5, "unknown"])
def test_invalid_limit_status_is_not_valid_coverage(invalid) -> None:
    snapshot = _market_snapshot()
    for row in snapshot.daily_basics:
        row.data = {"limit_status": invalid}
    overview, _ = MarketMartBuilder().build(snapshot, "v1")
    assert overview.payload["limitStats"]["coverage"]["valid"] == 0
    assert overview.payload["limitStats"]["riseLimit"] is None


def test_rise_ratio_is_not_mislabeled_as_complete_sentiment_score() -> None:
    overview, _ = MarketMartBuilder().build(_market_snapshot(), "v1")
    assert overview.payload["sentimentSnapshot"]["score"] is None
    assert overview.payload["sentimentSnapshot"]["riseRatio"] is None
    assert overview.payload["sentimentSnapshot"]["coverage"]["missing"] == 2
    assert overview.payload["sentimentSnapshot"]["level"] == "未计算"


def _industry_snapshot():
    snapshot = _market_snapshot()
    classifications = (
        Classification(id=1, provider="tushare", source="SW2021", classification_id="parent", name="消费", level=1),
        Classification(id=2, provider="tushare", source="SW2021", classification_id="leaf", name="食品", level=3, parent_id="parent"),
    )
    members = (
        ClassificationMember(classification_id=2, ts_code="600519.SH", effective_from=date(2020, 1, 1)),
        ClassificationMember(classification_id=2, ts_code="000001.SZ", effective_from=date(2020, 1, 1)),
    )
    return replace(snapshot, classifications=classifications, classification_members=members)


def test_leaf_membership_also_builds_parent_without_double_counting() -> None:
    snapshot = _industry_snapshot()
    direct = ClassificationMember(classification_id=1, ts_code="600519.SH", effective_from=date(2020, 1, 1))
    snapshot = replace(snapshot, classification_members=(*snapshot.classification_members, direct))
    _, explorer = MarketMartBuilder().build(snapshot, "v1")
    parent = explorer.payload["heatmapData"]["majorIndustry"][0]
    assert parent["name"] == "消费" and parent["stockCount"] == 2
    assert len(parent["stocks"]) == 2


def test_heatmap_preserves_missing_fields_and_averages_only_valid_changes() -> None:
    snapshot = _industry_snapshot()
    snapshot.daily_prices[0].pct_chg = None
    snapshot.daily_prices[0].amount = None
    snapshot.daily_basics[0].total_mv = None
    _, explorer = MarketMartBuilder().build(snapshot, "v1")
    sector = explorer.payload["heatmapData"]["subIndustry"][0]
    stock = next(row for row in sector["stocks"] if row["symbol"] == "600519.SH")
    assert stock["change"] is None and stock["marketCap"] is None and stock["turnover"] is None
    assert sector["change"] == -1
    assert sector["marketCap"] is None
    assert sector["turnover"] is None
    assert sector["coverage"] == {"change": 1, "marketCap": 0, "turnover": 1, "total": 2}
    assert validate_product_payload("market-explorer", explorer.payload) == ()


def test_industry_levels_do_not_mix_intermediate_groups_into_leaf_tiles() -> None:
    snapshot = _industry_snapshot()
    middle = Classification(id=3, provider="tushare", source="SW2021", classification_id="middle", name="中层行业", level=2, parent_id="parent")
    snapshot.classifications[1].parent_id = "middle"
    snapshot = replace(snapshot, classifications=(*snapshot.classifications, middle))
    _, explorer = MarketMartBuilder().build(snapshot, "v1")
    assert [row["name"] for row in explorer.payload["heatmapData"]["subIndustry"]] == ["食品"]


def test_shared_builder_does_not_mix_classification_sources():
    snapshot = _industry_snapshot()
    old_root = Classification(id=5, provider="tushare", source="SW2014", classification_id="parent", name="旧消费", level=1)
    old_leaf = Classification(id=6, provider="tushare", source="SW2014", classification_id="old-leaf", name="旧食品", level=3, parent_id="parent")
    old_member = ClassificationMember(classification_id=6, ts_code="600519.SH", effective_from=date(2020, 1, 1))
    snapshot = replace(snapshot, classifications=(*snapshot.classifications, old_root, old_leaf), classification_members=(*snapshot.classification_members, old_member))
    _, explorer = MarketMartBuilder().build(snapshot, "v1")
    sector = explorer.payload["heatmapData"]["majorIndustry"][0]
    assert sector["stockCount"] == 2
    assert [row["name"] for row in explorer.payload["heatmapData"]["subIndustry"]] == ["食品"]
