from copy import deepcopy
from dataclasses import dataclass
from datetime import date

import pytest

from app.db.models import RiskResult
from app.stocks.marts.stocks import StockWorkbenchMartBuilder, DerivedMartRows
from app.shared.contracts import DataMeta, ProviderSnapshot
from app.shared.errors import DataServiceError
from app.stocks.service import WorkbenchService
from backend.tests.marts.test_screener_builder import _core_snapshot


RULES = ("inventoryRisk", "goodwillRisk", "netDebtRisk", "cashProfitRisk",
         "valuationRisk", "dividendRisk", "cashDividendRisk")


def candidate_for(statuses):
    rows = tuple(RiskResult(ts_code="600519.SH", risk_id=key, as_of=date(2026, 8, 24),
        status=status, formula="existing", conclusion="existing conclusion", inputs={}, missing_fields=[])
        for key, status in zip(RULES, statuses))
    return StockWorkbenchMartBuilder().build(_core_snapshot(), "600519.SH", "v1",
        derived_rows=DerivedMartRows({}, {"600519.SH": rows}))


@pytest.mark.parametrize("statuses,score,known,level", [
    (["normal"] * 4 + ["unknown"] * 3, None, 4, "数据不足"),
    (["normal"] * 5 + ["unknown"] * 2, 100, 5, "部分覆盖"),
    (["normal"] * 7, 100, 7, "正常"),
    (["risk"] * 7, 0, 7, "高风险"),
    (["risk"] * 3 + ["normal"] * 2 + ["unknown"] * 2, 40, 5, "高风险（部分覆盖）"),
    (["risk"] * 3 + ["unknown"] * 4, None, 3, "数据不足（已发现高风险）"),
    (["normal"] * 6, 100, 6, "部分覆盖"),
    ([], None, 0, "数据不足"),
])
def test_mart_risk_summary_counts_missing_rules_as_unknown(statuses, score, known, level):
    risks = candidate_for(statuses).payload["risks"]
    assert risks["score"] == score
    assert risks["level"] == level
    assert risks["assessment"]["knownCount"] == known
    assert risks["assessment"]["totalCount"] == 7
    assert risks["assessment"]["unknownCount"] == 7 - known
    assert risks["assessment"]["missingRuleIds"] == list(RULES[len(statuses):])


@dataclass
class StoredWorkbench:
    snapshot: ProviderSnapshot

    def read_workbench(self, symbol):
        assert symbol == "600519.SH"
        return self.snapshot


def test_database_legacy_score_is_read_as_safe_summary_without_mutating_payload_or_meta():
    raw = candidate_for(["normal"] * 4 + ["unknown"] * 3).payload
    raw["risks"]["score"] = 100
    raw["risks"]["level"] = "正常"
    raw["risks"].pop("assessment", None)
    original = deepcopy(raw)
    meta = DataMeta(source="database", asOf="2026-08-24T00:00:00Z", version="old", stale=False)
    result = WorkbenchService(StoredWorkbench(ProviderSnapshot(raw, meta))).get_workbench("600519.SH")
    assert result.data["risks"]["score"] is None
    assert result.data["risks"]["level"] == "数据不足"
    assert result.data["risks"]["assessment"]["knownCount"] == 4
    assert result.data["risks"]["groups"] == original["risks"]["groups"]
    assert result.meta == meta
    assert raw == original


def test_duplicate_rule_is_rejected_instead_of_increasing_coverage():
    raw = candidate_for(["normal"] * 4 + ["unknown"] * 3).payload
    raw["risks"]["groups"][0]["items"].append(deepcopy(raw["risks"]["groups"][0]["items"][0]))
    meta = DataMeta(source="database", asOf="2026-08-24", version="old", stale=False)
    with pytest.raises(DataServiceError):
        WorkbenchService(StoredWorkbench(ProviderSnapshot(raw, meta))).get_workbench("600519.SH")


def test_unrecognized_rule_cannot_be_ignored_to_claim_full_normal_coverage():
    raw = candidate_for(["normal"] * 7).payload
    unexpected = deepcopy(raw["risks"]["groups"][0]["items"][0])
    unexpected.update(id="unexpectedRule", status="risk")
    raw["risks"]["groups"][0]["items"].append(unexpected)
    meta = DataMeta(source="database", asOf="2026-08-24", version="old", stale=False)
    with pytest.raises(DataServiceError) as error:
        WorkbenchService(StoredWorkbench(ProviderSnapshot(raw, meta))).get_workbench("600519.SH")
    assert error.value.code == "INVALID_SOURCE_PAYLOAD"
    assert error.value.message == "风险规则未识别"


@pytest.mark.parametrize("field,value", [
    ("inputs", None), ("inputs", {}), ("inputs", [None]),
    ("inputs", [{"label": "x", "value": 1}]), ("inputs", [{"value": "x"}]),
    ("missingFields", None), ("missingFields", [None]),
    *[(field, None) for field in ("title", "summary", "detail", "reportDate",
        "announcementDate", "reportType", "formula", "conclusion")],
    ("drilldown", {}), ("drilldown", {"tab": "statements", "statement": "income", "fields": [None]}),
])
def test_database_rejects_malformed_risk_detail_without_mutating_source(field, value):
    raw = candidate_for(["normal"] * 7).payload
    raw["risks"]["groups"][0]["items"][0][field] = value
    original = deepcopy(raw)
    meta = DataMeta(source="database", asOf="2026-08-24", version="old", stale=False)
    with pytest.raises(DataServiceError) as error:
        WorkbenchService(StoredWorkbench(ProviderSnapshot(raw, meta))).get_workbench("600519.SH")
    assert error.value.code == "INVALID_SOURCE_PAYLOAD"
    assert raw == original


@pytest.mark.parametrize("field,value", [("id", None), ("title", {}), ("description", None)])
def test_database_rejects_malformed_risk_group(field, value):
    raw = candidate_for(["normal"] * 7).payload
    raw["risks"]["groups"][0][field] = value
    meta = DataMeta(source="database", asOf="2026-08-24", version="old", stale=False)
    with pytest.raises(DataServiceError) as error:
        WorkbenchService(StoredWorkbench(ProviderSnapshot(raw, meta))).get_workbench("600519.SH")
    assert error.value.code == "INVALID_SOURCE_PAYLOAD"


def test_database_accepts_legitimate_empty_risk_detail_arrays():
    raw = candidate_for(["normal"] * 7).payload
    for group in raw["risks"]["groups"]:
        for item in group["items"]:
            item["inputs"] = []
            item["missingFields"] = []
    original = deepcopy(raw)
    meta = DataMeta(source="database", asOf="2026-08-24", version="old", stale=False)
    result = WorkbenchService(StoredWorkbench(ProviderSnapshot(raw, meta))).get_workbench("600519.SH")
    assert result.data["risks"]["score"] == 100
    assert result.data["risks"]["groups"] == raw["risks"]["groups"]
    assert raw == original
