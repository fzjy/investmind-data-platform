"""仅使用内存发布快照；隔离运行器禁止真实 HTTP/业务数据库。"""
from copy import deepcopy
import json
import math
from pathlib import Path
from statistics import pstdev

import pytest
from fastapi.testclient import TestClient
from fastapi import FastAPI

from backend.tests.support.application import create_app
from app.portfolio.routes import create_router
from app.shared.contracts import DataMeta, ProviderSnapshot


DATES = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]


class PublishedProvider:
    def __init__(self):
        self.calls = 0
        self.raw = {
            "assets": [{"id": "A", "type": "stock"}, {"id": "B", "type": "stock"},
                       {"id": "CNY", "type": "cash"}],
            "prices": {"A": [{"date": d, "price": p} for d, p in zip(DATES, [100, 110, 99, 120])],
                       "B": [{"date": d, "price": p} for d, p in zip(DATES, [100, 100, 110, 100])],
                       "CNY": [{"date": DATES[-1], "price": 1}]},
            "defaultState": {}, "snapshotLabel": "test", "storageKey": "test",
        }
        self.meta = DataMeta(source="database", asOf="2026-01-07", version="published-v9",
                             stale=True, staleReason="测试旧快照")

    def read_portfolio(self):
        self.calls += 1
        return ProviderSnapshot(self.raw, self.meta)


@pytest.fixture
def api():
    provider = PublishedProvider()
    # 独立路由复用项目真实异常处理器，避免新版 FastAPI 延迟 include_router 的路由重叠。
    app = FastAPI(exception_handlers=create_app().exception_handlers)
    app.include_router(create_router(provider=provider))
    return TestClient(app), provider


def post(api, holdings=None, **kwargs):
    return api[0].post("/api/data/portfolio/analysis", json={
        "holdings": holdings if holdings is not None else [{"assetId": "A", "weight": 2}, {"assetId": "B", "weight": 1}],
        **kwargs,
    })


def test_buy_hold_population_metrics_and_published_meta(api):
    before = deepcopy(api[1].raw)
    response = post(api)
    assert response.status_code == 200
    body = response.json()
    assert body["meta"] == api[1].meta.model_dump(by_alias=True)
    assert api[1].calls == 1 and api[1].raw == before
    data = body["data"]
    values = [1, 16 / 15, 77 / 75, 17 / 15]
    assert [p["value"] for p in data["nav"]] == pytest.approx(values)
    assert [h["weight"] for h in data["holdings"]] == pytest.approx([2 / 3, 1 / 3])
    returns = [b / a - 1 for a, b in zip(values, values[1:])]
    metrics = data["metrics"]
    assert metrics["totalReturn"] == pytest.approx(values[-1] - 1)
    assert metrics["annualReturn"] == pytest.approx(values[-1] ** (252 / 3) - 1)
    assert metrics["annualVolatility"] == pytest.approx(pstdev(returns) * math.sqrt(252))
    assert metrics["sharpe"] == pytest.approx(sum(returns) / 3 / pstdev(returns) * math.sqrt(252))
    assert metrics["maximumDrawdown"] == pytest.approx(77 / 80 - 1)
    assert data["effectiveWindow"] == {"startDate": DATES[0], "endDate": DATES[-1], "observations": 4}
    assert data["priceBasis"]["kind"] == "published-adjusted-price"
    assert data["priceBasis"]["totalReturnVerified"] is False
    for key in ("dividends", "attribution", "indexBenchmarks", "mpt"):
        assert data["capabilities"][key]["status"] == "missing"
        assert "发布快照" in data["capabilities"][key]["reason"]
    json.dumps(data, allow_nan=False)


def test_analysis_reports_published_attribution_inputs_without_claiming_complete_coverage():
    from app.portfolio.analysis import analyze_portfolio

    raw = PublishedProvider().raw
    raw["snapshotLabel"] = DATES[-1]
    raw["attributionInputs"] = dict(asOf=DATES[-1], method="ttm-profit-per-current-share",
        assets={symbol: dict(earnings=[], shares=[]) for symbol in ("A", "B")})
    result = analyze_portfolio(raw, {"holdings": [{"assetId": "A", "weight": 1}]})
    assert result["capabilities"]["attribution"]["status"] == "partial"


@pytest.mark.parametrize("patch", [
    {"holdings": []}, {"holdings": [{"assetId": "A", "weight": 0}]},
    {"holdings": [{"assetId": "A", "weight": -1}]},
    {"holdings": [{"assetId": "A", "weight": "NaN"}]},
    {"holdings": [{"assetId": "A", "weight": True}]},
    {"holdings": [{"assetId": "A", "weight": "1"}]},
    {"holdings": [{"assetId": "A", "weight": 1}] * 2},
    {"holdings": [{"assetId": "A", "weight": 1}] * 101},
    {"startDate": "20260102"}, {"startDate": "2026-02-30"},
    {"startDate": "2026-01-02T00:00:00Z"}, {"endDate": 123},
    {"startDate": "2026-02-01", "endDate": "2026-01-01"},
    {"startDate": "0001-01-01", "endDate": "9999-12-31"},
    {"startDate": "0001-01-01"}, {"unexpected": 1},
])
def test_invalid_request_uses_project_422_envelope(api, patch):
    response = post(api, **patch)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert response.json()["meta"]["requestId"]


@pytest.mark.parametrize("kwargs", [
    {"holdings": [{"assetId": "unknown", "weight": 1}]},
    {"benchmarkAssetId": "unknown"},
    {"holdings": [{"assetId": "unknown", "weight": 0}, {"assetId": "A", "weight": 1}]},
])
def test_unknown_asset_is_404(api, kwargs):
    response = post(api, **kwargs)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DATA_NOT_FOUND"


def test_cash_uses_actual_reference_calendar(api):
    data = post(api, [{"assetId": "A", "weight": 1}, {"assetId": "CNY", "weight": 1}]).json()["data"]
    assert [p["date"] for p in data["nav"]] == DATES
    assert [p["value"] for p in data["nav"]] == pytest.approx([1, 1.05, .995, 1.1])


def test_cash_without_reference_does_not_invent_days(api):
    api[1].raw["prices"] = {"CNY": [{"date": DATES[-1], "price": 1}]}
    data = post(api, [{"assetId": "CNY", "weight": 1}]).json()["data"]
    assert data["effectiveWindow"]["observations"] == 1
    assert all(value is None for value in data["metrics"].values())
    assert data["warnings"]


def test_inner_missing_price_disables_daily_metrics(api):
    api[1].raw["prices"]["B"].pop(1)
    data = post(api).json()["data"]
    assert data["metrics"]["totalReturn"] == pytest.approx(2 / 15)
    for key in ("annualReturn", "annualVolatility", "sharpe"):
        assert data["metrics"][key] is None
    assert data["warnings"]


def test_window_filter_and_no_overlap(api):
    data = post(api, startDate=DATES[1], endDate=DATES[2]).json()["data"]
    assert [p["date"] for p in data["nav"]] == DATES[1:3]
    assert data["metrics"]["annualVolatility"] is None
    data = post(api, startDate="2027-01-01").json()["data"]
    assert data["nav"] == []
    assert all(value is None for value in data["metrics"].values())


@pytest.mark.parametrize("price", [None, 0, -1, float("nan"), float("inf")])
def test_bad_published_prices_never_produce_nan(api, price):
    api[1].raw["prices"]["B"][1]["price"] = price
    response = post(api)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["metrics"]["annualVolatility"] is None
    assert data["warnings"]
    json.dumps(data, allow_nan=False)


def test_large_finite_relative_weights_normalize_without_overflow(api):
    data = post(api, [{"assetId": "A", "weight": 1e308}, {"assetId": "B", "weight": 1e308}]).json()["data"]
    assert [h["weight"] for h in data["holdings"]] == [.5, .5]


@pytest.mark.parametrize("body", ['{"holdings":[{"assetId":"A","weight":NaN}]}', '{}', 'null', '[]', '{'])
def test_malformed_or_nonfinite_json_is_422(api, body):
    response = api[0].post("/api/data/portfolio/analysis", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"


def test_default_app_wires_analysis_route():
    response = TestClient(create_app()).post("/api/data/portfolio/analysis", json={
        "holdings": [{"assetId": "CNY", "weight": 1}],
    })
    assert response.status_code == 200
    assert response.json()["meta"]["source"] == "fixture"


def test_pure_cash_uses_published_non_cash_days_and_zero_volatility(api):
    data = post(api, [{"assetId": "CNY", "weight": 1}], benchmarkAssetId="B").json()["data"]
    assert [point["date"] for point in data["nav"]] == DATES
    assert data["metrics"]["annualVolatility"] == 0
    assert data["metrics"]["annualReturn"] == 0
    assert data["metrics"]["sharpe"] is None


def test_zero_weight_is_omitted_and_not_used_for_calendar(api):
    api[1].raw["prices"]["B"] = []
    data = post(api, [{"assetId": "A", "weight": 1}, {"assetId": "B", "weight": 0}]).json()["data"]
    assert data["holdings"] == [{"assetId": "A", "weight": 1}]
    assert data["effectiveWindow"]["observations"] == 4


def test_disjoint_assets_and_duplicate_price_date_are_not_filled(api):
    api[1].raw["prices"]["B"] = [{"date": "2025-12-31", "price": 100}]
    assert post(api).json()["data"]["nav"] == []
    api[1].raw["prices"]["A"].append({"date": DATES[1], "price": 110})
    data = post(api, [{"assetId": "A", "weight": 1}]).json()["data"]
    assert data["metrics"]["annualVolatility"] is None
    assert data["warnings"]


def test_extreme_published_price_ratios_are_finite_or_missing(api):
    api[1].raw["prices"]["A"][0]["price"] = 1e-308
    api[1].raw["prices"]["A"][1]["price"] = 1e308
    response = post(api)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["warnings"]
    json.dumps(data, allow_nan=False)


def test_single_holding_gap_is_detected_against_published_reference_calendar(api):
    api[1].raw["prices"]["A"].pop(1)
    data = post(api, [{"assetId": "A", "weight": 1}]).json()["data"]
    assert data["metrics"]["annualVolatility"] is None
    assert data["metrics"]["annualReturn"] is None
    assert data["warnings"]


def test_shared_settings_path_points_to_root_json():
    from app.portfolio.analysis import PORTFOLIO_SETTINGS_FILE
    assert PORTFOLIO_SETTINGS_FILE == Path(__file__).resolve().parents[2] / "config" / "portfolio.json"


def test_service_reads_changed_shared_settings_instead_of_fixed_constants(api, tmp_path, monkeypatch):
    from app.portfolio import analysis
    config = tmp_path / "portfolio.json"
    config.write_text(json.dumps({"annualizationDays": 12, "apiMaxAssets": 1}), encoding="utf-8")
    monkeypatch.setattr(analysis, "PORTFOLIO_SETTINGS_FILE", config)
    assert post(api).status_code == 422
    assert api[1].calls == 0
    response = post(api, [{"assetId": "A", "weight": 1}])
    assert response.status_code == 200
    metrics = response.json()["data"]["metrics"]
    returns = [.1, -.1, 120 / 99 - 1]
    assert metrics["annualReturn"] == pytest.approx(1.2 ** 4 - 1)
    assert metrics["annualVolatility"] == pytest.approx(pstdev(returns) * math.sqrt(12))
    assert metrics["sharpe"] == pytest.approx(sum(returns) / 3 / pstdev(returns) * math.sqrt(12))
    config.write_text(json.dumps({"annualizationDays": 24, "apiMaxAssets": 2}), encoding="utf-8")
    response = post(api)
    assert response.status_code == 200
    assert response.json()["data"]["metrics"]["annualReturn"] == pytest.approx((17 / 15) ** 8 - 1)


@pytest.mark.parametrize("config", [
    {"annualizationDays": 0, "apiMaxAssets": 100},
    {"annualizationDays": True, "apiMaxAssets": 100},
    {"annualizationDays": "252", "apiMaxAssets": 100},
    {"annualizationDays": 252, "apiMaxAssets": -1},
    {"annualizationDays": 252, "apiMaxAssets": 1.5},
    {"annualizationDays": 252, "apiMaxAssets": float("inf")},
    {"annualizationDays": 367, "apiMaxAssets": 100},
    {"annualizationDays": 252, "apiMaxAssets": 10001},
    {"annualizationDays": 252},
])
def test_invalid_shared_settings_fail_explicitly_without_reading_snapshot(api, tmp_path, monkeypatch, config):
    from app.portfolio import analysis
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(analysis, "PORTFOLIO_SETTINGS_FILE", path)
    response = post(api)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert api[1].calls == 0


def test_root_serves_published_treasury_revisions_without_collapsing_history(api):
    day = '2026-10-20'
    first = dict(date=day, value=.018, releasedAt=day + 'T00:00:00Z', revision='v1', sourceId='core:1')
    second = first | dict(value=.019, releasedAt='2026-10-21T00:00:00Z', revision='v2', sourceId='core:2')
    reference = dict(status='observed', asOf='2026-10-21', seriesId='cn.rate.govbond_10y',
                     unit='annual-rate', points=[second], revisions=[first, second])
    api[1].raw.update(treasuryReference=reference, snapshotLabel='2026-10-21')
    response = api[0].get('/api/data/portfolio')
    assert response.status_code == 200
    assert response.json()['data']['treasuryReference'] == reference
    assert len(reference['revisions']) == 2
