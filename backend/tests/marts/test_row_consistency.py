"""真实 Builder 的页面与结构化行必须一致；失败不可替换旧发布。"""
from copy import deepcopy
from dataclasses import replace
from datetime import date
import json

import pytest
from sqlalchemy import select

from app.db.models import (PageSnapshot, PublishedSnapshot, QualityCheckResult,
                           StockDailyChart, StockScreenerDaily, StockWorkbenchSnapshot)
from app.jobs.build_and_publish import BuildAndPublishOptions
from app.jobs.build_history import SqlBuildHistory
from app.marts.contracts import MartRows, QualityContext
from app.marts.publication import QualityCheckFailed, SnapshotPublisher
from app.marts.quality import QualityGate

from backend.tests.marts.test_finite_quality import candidates, checks


def consistency(items):
    return next((result for result in checks(items)
                 if result.name == "payload-rows-consistent"), None)


def assert_blocked(items):
    result = consistency(items)
    assert result is not None and not result.passed
    assert result.severity == "blocking"
    assert result.details["issues"]
    json.dumps(result.details, allow_nan=False)


def stored_publication(factory):
    """同时检查当前指针、页面历史及三类结构化历史的全部列，不仅版本名。"""
    with factory() as session:
        return {
            model.__tablename__: [tuple(deepcopy(getattr(row, column.key))
                                        for column in model.__table__.columns)
                                  for row in session.scalars(select(model).order_by(
                                      *model.__table__.primary_key.columns))]
            for model in (PageSnapshot, PublishedSnapshot, StockScreenerDaily,
                          StockWorkbenchSnapshot, StockDailyChart)
        }


@pytest.mark.parametrize("side", ["payload", "rows"])
@pytest.mark.parametrize("failure", ["duplicate", "empty", "symbol", "price", "nullable", "missing-field", "boolean"])
def test_screener_rejects_single_side_key_count_and_field_mismatch(side, failure):
    screener, workbench = candidates()
    stocks = deepcopy(screener.payload["stocks"])
    if failure == "duplicate":
        stocks.append(deepcopy(stocks[0]))
    elif failure == "empty":
        stocks.clear()
    elif failure == "missing-field":
        del stocks[0]["pe"]
    else:
        field, value = {"symbol": ("symbol", "000001.SZ"), "price": ("price", 1499),
                        "nullable": ("pe", None), "boolean": ("price", True)}[failure]
        stocks[0][field] = value
    if side == "payload":
        screener = replace(screener, payload={**screener.payload, "stocks": stocks})
    else:
        screener = replace(screener, rows=replace(screener.rows, screener=tuple(stocks)))
    assert_blocked((screener, workbench))


def test_duplicate_screener_on_both_sides_is_not_hidden_by_equal_sets():
    screener, workbench = candidates()
    stocks = [*screener.payload["stocks"], deepcopy(screener.payload["stocks"][0])]
    screener = replace(screener, payload={**screener.payload, "stocks": stocks},
                       rows=replace(screener.rows, screener=tuple(stocks)))
    assert_blocked((screener, workbench))


@pytest.mark.parametrize("product", ["screener", "workbench"])
@pytest.mark.parametrize("side", ["payload", "rows"])
def test_non_string_field_names_return_json_safe_blocking_result(product, side):
    screener, workbench = candidates()
    if product == "screener":
        stocks = deepcopy(screener.payload["stocks"])
        stocks[0][0] = "unexpected-field"
        if side == "payload":
            screener = replace(screener, payload={**screener.payload, "stocks": stocks})
        else:
            screener = replace(screener, rows=replace(screener.rows, screener=tuple(stocks)))
    else:
        row = deepcopy(workbench.payload)
        row[0] = "unexpected-field"
        if side == "payload":
            workbench = replace(workbench, payload=row)
        else:
            workbench = replace(workbench, rows=replace(workbench.rows, workbenches=(row,)))
    assert_blocked((screener, workbench))


@pytest.mark.parametrize("failure", ["product", "payload-symbol", "quote-symbol", "row-symbol", "empty-row", "duplicate-row", "row-price"])
def test_workbench_identity_and_snapshot_match(failure):
    screener, workbench = candidates()
    payload = deepcopy(workbench.payload)
    row = deepcopy(workbench.rows.workbenches[0])
    if failure == "product":
        workbench = replace(workbench, product="stock-workbench:000001.SZ")
    elif failure == "payload-symbol":
        payload["symbol"] = "000001.SZ"
    elif failure == "quote-symbol":
        payload["quote"]["symbol"] = "000001.SZ"
    elif failure == "row-symbol":
        row["symbol"] = "000001.SZ"
    elif failure == "row-price":
        row["quote"]["price"] = 1499
    rows = () if failure == "empty-row" else (row, row) if failure == "duplicate-row" else (row,)
    workbench = replace(workbench, payload=payload,
                        rows=replace(workbench.rows, workbenches=rows))
    assert_blocked((screener, workbench))


@pytest.mark.parametrize("side", ["payload", "rows"])
@pytest.mark.parametrize("failure", ["duplicate", "empty", "date", "open", "high", "low", "close", "volume"])
def test_candles_reject_duplicates_missing_days_and_different_values(side, failure):
    screener, workbench = candidates()
    candles = deepcopy(list(workbench.rows.charts))
    if failure == "duplicate":
        candles.append(deepcopy(candles[0]))
    elif failure == "empty":
        candles.clear()
    else:
        candles[0][failure] = {"date": "2026-08-23", "open": 1491, "high": 1511,
                               "low": 1479, "close": 1499, "volume": 101}[failure]
    if side == "rows":
        workbench = replace(workbench, rows=replace(workbench.rows, charts=tuple(candles)))
    else:
        payload = deepcopy(workbench.payload)
        payload["dailyChart"]["candles"] = candles
        # 同步页面与工作台快照，仅隔离蜡烛结构化行的差异。
        workbench = replace(workbench, payload=payload,
                            rows=replace(workbench.rows, workbenches=(payload,)))
    assert_blocked((screener, workbench))


def test_chart_date_aliases_cannot_hide_duplicate_normalized_dates():
    screener, workbench = candidates()
    candle = deepcopy(workbench.rows.charts[0])
    candle["trade_date"] = date.fromisoformat(candle.pop("date"))
    workbench = replace(workbench, rows=replace(workbench.rows, charts=(*workbench.rows.charts, candle)))
    assert_blocked((screener, workbench))


@pytest.mark.parametrize("value", [None, 0, -1, -1.25])
def test_matching_nullable_and_finite_negative_zero_values_remain_valid(value):
    screener, workbench = candidates()
    screener.payload["stocks"][0]["pe"] = value
    result = consistency((screener, workbench))
    assert result is not None and result.passed
    assert all(result.passed for result in checks((screener, workbench)) if result.severity == "blocking")


def test_valid_independent_copies_reordered_symbols_candles_and_date_aliases():
    screener, workbench = candidates()
    stocks = deepcopy(screener.payload["stocks"])
    stocks.append({**deepcopy(stocks[0]), "symbol": "000001.SZ"})
    screener = replace(screener, payload={**screener.payload, "stocks": stocks},
                       rows=MartRows(screener=tuple(reversed(deepcopy(stocks)))))
    payload = deepcopy(workbench.payload)
    candles = payload["dailyChart"]["candles"]
    candles.append({**deepcopy(candles[0]), "date": "2026-08-23"})
    charts = deepcopy(candles)
    for candle in charts:
        candle["tradeDate"] = date.fromisoformat(candle.pop("date"))
        candle["volume"] = float(candle["volume"])
    workbench = replace(workbench, payload=payload,
                        rows=MartRows(workbenches=(deepcopy(payload),), charts=tuple(reversed(charts))))
    result = consistency((screener, workbench))
    assert result is not None and result.passed
    assert all(result.passed for result in checks((screener, workbench)) if result.severity == "blocking")


def test_empty_eligible_range_without_requested_workbenches_remains_valid():
    screener, _ = candidates()
    screener = replace(screener, payload={**screener.payload, "stocks": []}, rows=MartRows())
    results = QualityGate().evaluate((screener,), QualityContext(0, (), 0, 0, ()))
    assert all(result.passed for result in results if result.severity == "blocking")


@pytest.mark.parametrize("failure", ["screener", "workbench", "chart"])
def test_quality_history_and_old_published_snapshots_survive_consistency_failure(pg_factory, failure):
    factory = pg_factory
    publisher = SnapshotPublisher(factory)
    original = candidates()
    publisher.publish_many(original, "consistent-old", checks(original))
    before = stored_publication(factory)
    screener, workbench = candidates()
    if failure == "screener":
        row = {**screener.rows.screener[0], "price": 1499}
        screener = replace(screener, rows=replace(screener.rows, screener=(row,)))
    elif failure == "workbench":
        row = deepcopy(workbench.rows.workbenches[0])
        row["quote"]["price"] = 1499
        workbench = replace(workbench, rows=replace(workbench.rows, workbenches=(row,)))
    else:
        row = {**workbench.rows.charts[0], "volume": 101}
        workbench = replace(workbench, rows=replace(workbench.rows, charts=(row,)))
    items = (screener, workbench)
    assert_blocked(items)
    quality = checks(items)
    history = SqlBuildHistory(factory)
    run_id = history.start(BuildAndPublishOptions(date(2026, 8, 24), ("600519.SH",)))
    history.record_quality(run_id, quality)
    with pytest.raises(QualityCheckFailed):
        publisher.publish_many(items, "inconsistent-new", quality)
    assert stored_publication(factory) == before
    with factory() as session:
        assert set(session.scalars(select(PageSnapshot.snapshot_version))) == {"consistent-old"}
        recorded = session.scalar(select(QualityCheckResult).where(
            QualityCheckResult.job_run_id == run_id,
            QualityCheckResult.check_name == "payload-rows-consistent"))
        assert recorded is not None and not recorded.passed
        json.dumps(recorded.details, allow_nan=False)
    for product in ("screener", "stock-workbench:600519.SH"):
        assert publisher.current(product).version == "consistent-old"
