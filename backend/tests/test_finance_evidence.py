from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
import sqlite3

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db.models import IngestionBatch, MetricInput, MetricValue, PageSnapshot, SourcePayload
from app.finance_mcp.evidence import evidence_catalog, expand_evidence
from app.ingestion.repositories import content_hash
from app.shared.errors import DataServiceError
from app.stocks.derived.workbench_cards import display_card


SYMBOL = "600519.SH"
VERSION = "sample-archive"
FISCAL = "workbenchFiscalDividendYield"
ANNUAL = "workbenchAnnualDividendYield"
NTM = "workbenchNtmDividendYield"
DAY = date(2026, 10, 21)
ROW = "raw.source_payload:8:row:1"


def calculation(refs):
    return dict(formula="已保存的公式", formulaVersion="v2", asOf=DAY.isoformat(),
                rows=[{"label": "计算结果", "value": "0.0000000000 %"}],
                missing=[], sourceIds=refs)


@pytest.fixture
def archive():
    # 显式构造纯内存连接，离线入口始终禁止未登记的业务数据库连接。
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(":memory:"))
    with engine.begin() as connection:
        for schema in ("mart", "raw", "derived"):
            connection.exec_driver_sql(f"ATTACH ':memory:' AS {schema}")
        for model in (PageSnapshot, IngestionBatch, SourcePayload, MetricValue, MetricInput):
            model.__table__.create(connection)
    factory = sessionmaker(engine)
    calc = calculation([ROW])
    cards = [display_card(metric, DAY.year, Decimal(0), calc) for metric in (FISCAL, ANNUAL, NTM)]
    data = {"symbol": SYMBOL, "metrics": {"cards": [*cards, {"label": "expectedDividendYield", "value": 77}]}}
    payload = {"fields": ["ts_code", "end_date", "cash_div_tax", "internal_note"],
               "items": [[SYMBOL, "20251231", 0, "private"], ["000001.SZ", "20251231", 1999, "private"]]}
    captured = datetime(2026, 11, 12, tzinfo=timezone.utc)
    with factory.begin() as session:
        session.add(PageSnapshot(product="stock-workbench:" + SYMBOL, snapshot_version=VERSION,
                                 as_of=datetime(2026, 10, 21, tzinfo=timezone.utc), payload=data))
        session.add(IngestionBatch(id="batch", source="tushare", dataset="dividend", status="completed"))
        session.add(SourcePayload(id=8, batch_id="batch", api_name="dividend", request_hash="a"*64,
                                  content_hash=content_hash(payload), payload=payload, captured_at=captured))
        for index, metric in enumerate((FISCAL, ANNUAL, NTM), 1):
            session.add(MetricValue(id=index, ts_code=SYMBOL, metric_id=metric, as_of=DAY, value=Decimal(0),
                                    unit="%", formula_version="v2", quality="ok", calculation=calc))
            session.add(MetricInput(metric_value_id=index, input_name="source", source_table="raw.source_payload",
                                    source_record_key="8:row:1", content_hash="b"*64))
    yield factory, data
    engine.dispose()


def expand(archive, metric=FISCAL):
    factory, data = archive
    ref, = evidence_catalog(SYMBOL, VERSION, data, [metric])
    return expand_evidence(factory, SYMBOL, VERSION, ref["evidenceRef"])


def test_catalog_uses_canonical_selected_cards_and_separates_estimates(archive):
    _, data = archive
    refs = evidence_catalog(SYMBOL, VERSION, data)
    assert [(r["metricId"], r["kind"]) for r in refs] == [(FISCAL, "fact"), (ANNUAL, "estimate"), (NTM, "estimate")]
    assert evidence_catalog(SYMBOL, VERSION, data, []) == []
    assert [r["metricId"] for r in evidence_catalog(SYMBOL, VERSION, data, [NTM])] == [NTM]


def test_permitted_row_and_dates_only_with_real_zero_and_source_units(archive):
    item = expand(archive)
    assert item["sourceId"] == "derived.metric_value:1"
    assert (item["value"], item["displayValue"], item["unit"]) == ("0.0000000000", "0.00%", "%")
    assert item["calculation"]["asOf"] == "2026-10-21"
    source, = item["originals"]
    assert source["capturedAt"].startswith("2026-11-12")
    assert source["rows"] == [{"sourceId": ROW, "fields": {"ts_code": SYMBOL, "end_date": "20251231", "cash_div_tax": 0}}]
    assert source["fieldUnits"]["cash_div_tax"] == "元/股"
    assert item["inputs"][0]["contentHash"] == "b"*64


@pytest.mark.parametrize("target", ["wrong-symbol", "missing-version", "other-version", "unregistered", "changed-card"])
def test_ref_never_expands_another_scope_or_latest(archive, target):
    factory, data = archive
    ref = evidence_catalog(SYMBOL, VERSION, data)[0]["evidenceRef"]
    symbol = "000001.SZ" if target == "wrong-symbol" else SYMBOL
    version = "missing" if target == "missing-version" else "other" if target == "other-version" else VERSION
    if target in ("wrong-symbol", "other-version", "changed-card"):
        changed = deepcopy(data)
        changed["symbol"] = symbol
        with factory.begin() as session:
            if target == "changed-card":
                changed["metrics"]["cards"][0]["value"] = "9.9%"
                session.scalar(select(PageSnapshot)).payload = changed
            else:
                session.add(PageSnapshot(product="stock-workbench:"+symbol, snapshot_version=version,
                                         as_of=datetime(2026, 10, 21), payload=changed))
    with pytest.raises(DataServiceError) as error:
        expand_evidence(factory, symbol, version, ROW if target == "unregistered" else ref)
    assert error.value.code == "DATA_NOT_FOUND"


@pytest.mark.parametrize("mutation", ["calculation", "precise-value", "business-date", "formula-version"])
def test_recalculated_derived_record_cannot_supply_historical_precision(archive, mutation):
    factory, _ = archive
    with factory.begin() as session:
        record = session.get(MetricValue, 1)
        if mutation == "calculation":
            record.calculation = {**record.calculation, "formula": "后来的公式"}
        elif mutation == "precise-value":
            record.value = Decimal("0.0000000123")
        elif mutation == "business-date":
            record.as_of = date(2026, 10, 22)
        else:
            record.formula_version = "new"
    result = expand(archive)
    assert result["displayValue"] == "0.00%"
    assert result["value"] is None and result["sourceId"] is None
    assert result["gaps"]


@pytest.mark.parametrize("mutation", ["absent", "other-security", "forged-content", "unrelated-dataset"])
def test_cannot_disclose_out_of_scope_or_missing_original(archive, mutation):
    factory, _ = archive
    with factory.begin() as session:
        raw = session.get(SourcePayload, 8)
        if mutation == "absent":
            session.delete(raw)
        elif mutation == "other-security":
            payload = deepcopy(raw.payload)
            payload["items"][0][0] = "000001.SZ"
            raw.payload = payload
            raw.content_hash = content_hash(payload)
        elif mutation == "unrelated-dataset":
            session.get(IngestionBatch, "batch").dataset = "user-private"
        else:
            raw.content_hash = "f"*64
    item = expand(archive)
    assert item["originals"] == []
    assert item["gaps"]
    assert item["value"] == "0.0000000000"


def test_excessive_selected_card_fails_closed(archive):
    _, data = archive
    data["metrics"]["cards"][0]["calculation"] = calculation([f"raw.source_payload:{i}" for i in range(1000)])
    with pytest.raises(DataServiceError) as error:
        evidence_catalog(SYMBOL, VERSION, data, [FISCAL])
    assert error.value.code == "INVALID_REQUEST"


def test_oversize_unselected_card_does_not_block_the_selected_metric(archive):
    factory, data = archive
    data["metrics"]["cards"][0]["calculation"] = calculation([f"raw.source_payload:{i}" for i in range(1000)])
    with factory.begin() as session:
        session.scalar(select(PageSnapshot)).payload = data
    assert expand(archive, ANNUAL)["value"] == "0.0000000000"


def test_reference_cannot_expand_after_an_ambiguous_duplicate_appears(archive):
    factory, data = archive
    ref = evidence_catalog(SYMBOL, VERSION, data)[0]["evidenceRef"]
    data["metrics"]["cards"].append(deepcopy(data["metrics"]["cards"][0]))
    with factory.begin() as session:
        session.scalar(select(PageSnapshot)).payload = data
    with pytest.raises(DataServiceError) as error:
        expand_evidence(factory, SYMBOL, VERSION, ref)
    assert error.value.code == "INVALID_SOURCE_PAYLOAD"


def test_excessive_projected_raw_row_does_not_reach_the_model(archive):
    factory, _ = archive
    with factory.begin() as session:
        raw = session.get(SourcePayload, 8)
        payload = deepcopy(raw.payload)
        payload["items"][0][1] = "x"*120_000
        raw.payload = payload
        raw.content_hash = content_hash(payload)
    with pytest.raises(DataServiceError) as error:
        expand(archive)
    assert error.value.code == "INVALID_REQUEST"
