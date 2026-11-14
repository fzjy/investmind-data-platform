from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import json
from decimal import Decimal
from pathlib import Path

import httpx

from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


AS_OF = datetime(2026, 9, 20, 8, tzinfo=UTC)
TEST_FRED_KEY = "task11-fred-secret-canary"
TEST_TUSHARE_TOKEN = "task11-tushare-secret-canary"
CATALOG_VERSION = "task11-controlled-only-v1"
FALLBACK_ID = "cn.cpi.yoy"
MISSING_ID = "cn.cpi.core_yoy"
STALE_ID = "cn.ppi.yoy"
REVISION_ID = "us.cpi.yoy"
NONFINITE_ID = "us.labor.unemployment_rate"
NBS_URL = "https://www.stats.gov.cn/sj/zxfb/202609/t20260915_1234567.html"
PERIODS = {
    "monthly": (date(2026, 6, 1), date(2026, 7, 1), date(2026, 8, 1)),
    "quarterly": (date(2026, 1, 1), date(2026, 4, 1), date(2026, 7, 1)),
    "daily": (date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)),
}


def write_controlled_catalog(path: Path) -> MacroSeriesCatalog:
    # 仅测试配置：不是公开来源/许可验证，不改变默认目录的启用状态。
    payload = json.loads(DEFAULT_MACRO_CATALOG_FILE.read_text(encoding="utf-8"))
    payload["catalogVersion"] = CATALOG_VERSION
    payload["defaults"]["maxAttempts"] = 1
    payload["defaults"]["retryBackoffSeconds"] = [0]
    for series in payload["series"]:
        # 固定旧场景的六项离线来源，用于回归来源恢复/回退；新 NBS 默认来源另有端到端测试。
        if series["id"] in {
            "cn.cpi.core_yoy", "cn.industry.value_added_yoy", "cn.credit.tsf_stock_yoy",
            "cn.rate.dr007", "cn.rate.govbond_10y", "cn.fx.usdcny",
        }:
            series["sources"] = []
        if series["id"] == FALLBACK_ID:
            series["sources"][0]["priority"] = 2
            series["sources"].insert(0, {
                "provider": "nbs", "priority": 1, "series": "居民消费价格",
                "field": "同比涨跌幅（%）", "transform": "identity",
                "sourceUrl": NBS_URL,
                "termsUrl": "https://www.stats.gov.cn/",
                "accessMode": "public_page", "displayAllowed": True,
                "responseFields": ["ArticleTitle", "PubDate", "居民消费价格", "同比涨跌幅（%）"],
            })
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return MacroSeriesCatalog.load(path)


@dataclass(frozen=True)
class RecordedRequest:
    provider: str
    series_id: str
    start: date | None
    end: date | None
    as_of: date | None
    fields: tuple[str, ...] = ()


class ControlledMacroResponses:
    def __init__(self, catalog: MacroSeriesCatalog) -> None:
        self.catalog = catalog
        self.stage = "first"
        self.now = AS_OF
        self.requests: list[RecordedRequest] = []
        self.blocked_ids: set[str] = set()
        self.reject_nonfinite = False
        self.unexpected_requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "markets.newyorkfed.org" and request.url.path == "/api/rates/unsecured/effr/search.json":
            self.requests.append(RecordedRequest("nyfed", "us.rate.fed_funds", None, None, None))
            return httpx.Response(503, json={"error": "controlled historical fallback unavailable"})
        if request.url.host == "home.treasury.gov" and request.url.path == "/resource-center/data-chart-center/interest-rates/pages/xml":
            self.requests.append(RecordedRequest("us_treasury", "us.rate.treasury_10y", None, None, None))
            return httpx.Response(503, text="controlled historical fallback unavailable")
        if request.url.host == "api.tushare.pro" and request.url.path == "/":
            return self._tushare(request)
        if request.url.host == "api.stlouisfed.org" and request.url.path == "/fred/series/observations":
            return self._fred(request)
        if request.url.host == "api.stlouisfed.org" and request.url.path == "/fred/series/vintagedates":
            params = request.url.params
            assert params["api_key"] == TEST_FRED_KEY
            series = next(s for s in self.catalog.series if any(
                source.provider == "fred" and source.series == params["series_id"] for source in s.sources))
            start, end = date.fromisoformat(params["realtime_start"]), date.fromisoformat(params["realtime_end"])
            self.requests.append(RecordedRequest("fred-vintages", series.id, start, end, end))
            dates = [value for value in ("2026-09-15", "2026-09-20") if start <= date.fromisoformat(value) <= end]
            offset = int(params["offset"])
            return httpx.Response(200, json={"vintage_dates": dates[offset:offset + 1], "offset": offset, "count": len(dates), "limit": 1})
        if str(request.url) == NBS_URL:
            assert request.method == "GET"
            self.requests.append(RecordedRequest("nbs", FALLBACK_ID, None, None, None))
            if self.stage == "first" or FALLBACK_ID in self.blocked_ids:
                return httpx.Response(503, text=f"token={TEST_TUSHARE_TOKEN}")
            return httpx.Response(200, text='''<html><head>
                <meta name="ArticleTitle" content="2026年8月份居民消费价格同比上涨3.0%">
                <meta name="PubDate" content="2026/09/15 09:00">
                </head><body>2026年8月份，全国居民消费价格同比上涨3.0%。</body></html>''')
        self.unexpected_requests.append(str(request.url.copy_with(query=None)))
        raise AssertionError("未登记的 Task11 受控请求")

    def _tushare(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload["token"] == TEST_TUSHARE_TOKEN
        fields = tuple(payload["fields"].split(","))
        matches = [
            (series, source) for series in self.catalog.series for source in series.sources
            if source.provider == "tushare" and source.api == payload["api_name"]
            and source.response_fields == fields
        ]
        assert len(matches) == 1
        series, source = matches[0]
        params = payload["params"]
        if series.frequency == "monthly":
            assert set(params) == {"start_m", "end_m"}
            start = datetime.strptime(params["start_m"], "%Y%m").date()
            import calendar
            end_first = datetime.strptime(params["end_m"], "%Y%m").date()
            end = end_first.replace(day=calendar.monthrange(end_first.year, end_first.month)[1])
        elif series.frequency == "quarterly":
            assert set(params) == {"start_q", "end_q"}
            start = date(int(params["start_q"][:4]), (int(params["start_q"][-1]) - 1) * 3 + 1, 1)
            end = date(int(params["end_q"][:4]), int(params["end_q"][-1]) * 3, 28)
        else:
            assert set(params) == {"start_date", "end_date"}
            start = datetime.strptime(params["start_date"], "%Y%m%d").date()
            end = datetime.strptime(params["end_date"], "%Y%m%d").date()
        self.requests.append(RecordedRequest("tushare", series.id, start, end, None, fields))
        if (
            series.id in self.blocked_ids
            or (self.stage == "first" and series.id == MISSING_ID)
            or (self.stage == "second" and series.id == STALE_ID)
        ):
            return httpx.Response(200, json={
                "code": -2001, "msg": f"token={TEST_TUSHARE_TOKEN}; api_key={TEST_FRED_KEY}",
                "data": {"fields": list(fields), "items": []},
            })
        items = []
        for index, period in enumerate(PERIODS[series.frequency]):
            if not start <= period <= end:
                continue
            if series.frequency == "quarterly":
                raw_date = f"{period.year}Q{(period.month - 1) // 3 + 1}"
            else:
                raw_date = period.strftime("%Y%m" if series.frequency == "monthly" else "%Y%m%d")
            value = f"{index + 1}.1"
            if series.id == "cn.money.m1_yoy" and index == 2 and self.stage != "first":
                value = "3.6"
            items.append([raw_date, value, TEST_TUSHARE_TOKEN])
        # 回显额外秘密列，真实 client/provider 必须舍弃未请求列。
        return httpx.Response(200, json={
            "code": 0, "msg": "", "data": {"fields": [*fields, "token"], "items": items},
        })

    def _fred(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        params = request.url.params
        assert params["api_key"] == TEST_FRED_KEY
        matches = [
            (series, source) for series in self.catalog.series for source in series.sources
            if source.provider == "fred" and source.series == params["series_id"]
        ]
        assert len(matches) == 1
        series, source = matches[0]
        assert params["units"] == "lin"
        assert params["file_type"] == "json"
        start, end = date.fromisoformat(params["observation_start"]), date.fromisoformat(params["observation_end"])
        as_of = date.fromisoformat(params["realtime_end"])
        self.requests.append(RecordedRequest("fred", series.id, start, end, as_of))
        if series.id in self.blocked_ids:
            return httpx.Response(503, json={"error": f"api_key={TEST_FRED_KEY}"})
        observations = [
            {"date": period.isoformat(), "value": f"{index + 1}.1",
             "realtime_start": "2026-09-18" if series.frequency == "daily" else "2026-09-15",
             "realtime_end": "9999-12-31"}
            for index, period in enumerate(PERIODS[series.frequency]) if start <= period <= end
        ]
        if source.transform == "pc1":
            for row in observations:
                row["value"] = str(Decimal("100") + Decimal(row["value"]))
            observations.extend([
                {"date": period.replace(year=period.year - 1).isoformat(), "value": "100",
                 "realtime_start": "2026-09-15", "realtime_end": "9999-12-31"}
                for period in PERIODS["monthly"] if start <= period.replace(year=period.year - 1) <= end
            ])
        elif source.transform == "chg":
            cumulative = Decimal("1000")
            observations = [{"date": "2026-05-01", "value": str(cumulative),
                             "realtime_start": "2026-09-15", "realtime_end": "9999-12-31"}]
            for index, period in enumerate(PERIODS["monthly"]):
                cumulative += Decimal(f"{index + 1}.1")
                observations.append({"date": period.isoformat(), "value": str(cumulative),
                    "realtime_start": "2026-09-15", "realtime_end": "9999-12-31"})
            observations = [row for row in observations if start <= date.fromisoformat(row["date"]) <= end]
        if self.reject_nonfinite and series.id == NONFINITE_ID:
            observations.append({
                "date": "2026-08-01", "value": "NaN",
                "realtime_start": "2026-09-15", "realtime_end": "9999-12-31",
            })
        if series.id == REVISION_ID and self.stage != "first" and start <= date(2026, 8, 1) <= end:
            for row in observations:
                if row["date"] == "2026-08-01":
                    row["realtime_end"] = "2026-09-19"
            observations.extend([
                {"date": "2026-08-01", "value": "103.4", "realtime_start": "2026-09-20", "realtime_end": "2026-09-20"},
                {"date": "2026-08-01", "value": "109.9", "realtime_start": "2026-09-21", "realtime_end": "9999-12-31"},
            ])
        offset = int(params["offset"])
        return httpx.Response(200, json={
            "observations": observations[offset:offset + 2], "count": len(observations), "offset": offset,
            "api_key": TEST_FRED_KEY,
            "diagnostic": f"api_key={TEST_FRED_KEY}; token={TEST_TUSHARE_TOKEN}",
            "nested": {"token": TEST_TUSHARE_TOKEN},
        })
