"""凭证对照必须保留同键异文，并独立核对冻结响应的动态字段。"""

import base64
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys
from uuid import uuid4

import pytest
from app.portfolio.dividend_fields import DIVIDEND_FIELDS


@pytest.fixture
def audit(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    import foundation_audit_dividends as dividends
    import foundation_audit_products as products

    return dividends, products


def test_same_key_dividend_rows_are_order_independent(audit):
    dividends, _ = audit
    core_row = {
        "id": 17,
        "ts_code": "000001.SZ",
        "end_date": date(2025, 12, 31),
        "ann_date": date(2026, 3, 1),
        "div_proc": "实施",
        "cash_div": 0,
        "cash_div_tax": 1,
        "record_date": None,
        "ex_date": None,
        "pay_date": None,
        "data": {},
    }
    src = {
        "ts_code": "000001.SZ",
        "end_date": "20251231",
        "ann_date": "20260301",
        "div_proc": "实施",
        "cash_div": 0,
        "cash_div_tax": 1.0,
        "record_date": None,
        "ex_date": None,
        "pay_date": None,
        "imp_ann_date": None,
        "base_date": None,
        "base_share": 0,
    }
    old_legs = [(4, 1, src), (4, 2, src | {"cash_div_tax": 2, "base_share": 20})]
    new_leg = (8, 1, src | {"base_share": 30})
    inputs = old_legs + [new_leg]

    def compare(rows):
        return dividends.event_comparison([core_row], rows)

    result, evidence = compare(inputs)
    assert (result, evidence) == compare(reversed(inputs))
    assert result["coreVariantAgreement"] == {"some": 1}
    assert result["conflicts"] == {"cash_div_tax": 1}
    assert result["intraCaptureVariantKeys"] == result["crossCaptureChangedKeys"] == 1
    assert result["enrichedCoreIds"] == []
    assert evidence[0]["core"][0]["supplements"]["base_share"]["status"] == "different-values"
    assert {
        (ref["rawId"], n) for v in evidence[0]["variants"] for ref in v["sources"] for n in ref["rows"]
    } == {(4, 1), (4, 2), (8, 1)}
    consistent, evidence = compare([(4, 1, src), (8, 1, src)])
    assert consistent["coreVariantAgreement"] == {"all": 1}
    assert consistent["enrichedCoreIds"] == [17]
    assert evidence[0]["core"][0]["supplements"]["base_share"]["value"] == "0"
    with_gap, _ = compare([(4, 1, src), (8, 1, src | {"base_share": None})])
    assert with_gap["enrichedCoreIds"] == []


def test_dividend_http_precision_and_evidence_sources(audit):
    dividends, _ = audit
    low, high = "999999999999.12345678", "999999999999.12345679"
    fields = list(DIVIDEND_FIELDS)
    sample = dict.fromkeys(fields)
    sample.update(ts_code="A", end_date="20251231", ann_date="20260301", div_proc="实施")
    sample.update(cash_div="DECIMAL", base_share="DECIMAL")
    row_text = json.dumps([sample[f] for f in fields])
    items = "[" + ",".join(row_text.replace('"DECIMAL"', value) for value in (low, high)) + "]"
    facts = '{"fields":' + json.dumps(fields) + ',"items":' + items + "}"
    body = ('{"code":0,"data":' + facts + "}").encode()
    http = {
        "statusCode": 200,
        "credentialsRedacted": False,
        "bodyBase64": base64.b64encode(body).decode(),
        "byteLength": len(body),
        "bodySha256": sha256(body).hexdigest(),
    }
    rounded = json.loads(facts)
    assert rounded["items"][0] == rounded["items"][1]
    payload = rounded | {"_http": http}
    rows, method = dividends.business_rows(10, "portfolio_dividend_evidence", payload, json.dumps(payload))
    assert method == "http-bytes-decimal"
    assert [r["cash_div"] for r in rows] == [Decimal(low), Decimal(high)]
    assert all("sourceId" not in row for row in rows)
    assert dividends.business_rows_hash(rows[:1]) != dividends.business_rows_hash(rows[1:])
    assert dividends.business_rows_hash([{"x": Decimal("1.00")}]) == dividends.business_rows_hash([{"x": 1}])
    core = sample | {"id": 9, "cash_div": Decimal(low), "data": {}}
    summary, evidence = dividends.event_comparison(
        [core], [(10, 1, rows[0]), (10, 2, rows[1]), (20, 1, rows[1])]
    )
    assert summary["coreVariantAgreement"] == {"some": 1}
    assert summary["intraCaptureVariantKeys"] == summary["crossCaptureChangedKeys"] == 1
    event = evidence[0]
    assert event["core"][0]["supplements"]["base_share"]["observedValues"] == [low, high]
    assert [v["sources"] for v in event["variants"]] == [
        [{"rawId": 10, "rows": [1]}],
        [{"rawId": 10, "rows": [2]}, {"rawId": 20, "rows": [1]}],
    ]
    ordinary, source = dividends.business_rows(30, "dividend", rounded, facts)
    assert ordinary == rows and source == "stored-json-decimal"
    from foundation_audit_ranges import summarize

    class Captures:
        def execute(self, statement, params):
            if params["api"] != "dividend":
                return []
            return [
                (n, req, "portfolio_dividend_evidence", fields, json.dumps(rounded["items"]), http)
                for n, req in [(10, "first"), (20, "second")]
            ]

    cross_requests = summarize(Captures())["dividend"]
    assert cross_requests["overlapKeysWithDifferentValues"] == {"cash_div": 1, "base_share": 1}
    with pytest.raises(ValueError):
        dividends.business_rows(10, "portfolio_dividend_evidence", rounded, facts)
    http["bodySha256"] = "invalid"
    with pytest.raises(ValueError):
        dividends.business_rows(10, "portfolio_dividend_evidence", payload, json.dumps(payload))


@pytest.mark.parametrize("path", ["/api/v1/macro/insights", "/api/data/macro-insights"])
def test_only_nested_insights_clock_is_removed(audit, path):
    _, products = audit
    obj = {
        "readAt": "stored value",
        "collectionAudit": {"readAt": "2026-11-19T08:10:11+08:00", "status": "ok"},
    }
    assert products.normalize_dynamic(path, obj, set()) == ["collectionAudit.readAt"]
    assert obj == {"readAt": "stored value", "collectionAudit": {"status": "ok"}}
    with pytest.raises(ValueError):
        products.normalize_dynamic(path, {"collectionAudit": {"readAt": "2026-11-19T08:10:11"}}, set())


def test_search_uuid_is_v4_and_distinct_across_aliases(audit):
    _, products = audit
    seen = set()
    key = str(uuid4())
    assert products.normalize_dynamic("/api/v1/search", {"meta": {"requestId": key}}, seen) == [
        "meta.requestId"
    ]
    with pytest.raises(ValueError):
        products.normalize_dynamic("/api/data/search", {"meta": {"requestId": key}}, seen)
    with pytest.raises(ValueError):
        products.normalize_dynamic("/api/data/search", {"meta": {"requestId": "invalid-id"}}, seen)
