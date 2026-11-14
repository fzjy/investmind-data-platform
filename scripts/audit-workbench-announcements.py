"""在一个 PostgreSQL 只读快照内对照工作台事件、文档、采集窗口与供应商原件。"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.shared.snapshot_sql import PUBLISHED_SNAPSHOTS_SQL
from app.config import settings
from app.content.announcement_history import JOB
from app.content.content_policy import DISPLAY_STATUSES
from app.content.marts.report_details import previous_quarter, quarter_values, report_period
from app.db.models import ContentDocument, DisclosureSchedule, FinancialForecast, FinancialIndicator, IncomeStatement, SourcePayload
from app.ingestion.normalizers import normalize_disclosure_schedule
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository
from app.shared.trading_days import announcement_dates, candle_dates, load_trading_days
from app.stocks.database_provider import StocksDatabaseProvider
from app.stocks.service import WorkbenchService

ZONE = ZoneInfo("Asia/Shanghai")
FORECAST = ("type", "p_change_min", "p_change_max", "net_profit_min", "net_profit_max", "summary", "change_reason")


def source_indexes(session, codes):
    notices, news, forecasts = defaultdict(list), defaultdict(list), defaultdict(list)
    pages = defaultdict(list)
    for source in session.scalars(select(SourcePayload).where(SourcePayload.api_name == "hisAnnouncement/query")):
        body = source.payload
        pages[body["window"]].append((source.id, int(body["page"]), body["response"]))
        for row in body["response"]["announcements"]:
            notices[row["announcementId"]].append((source.id, row))
    for identity, payload in session.execute(text("""
        select id,payload from raw.external_record
        where record_type='content' and exists (
          select 1 from json_array_elements_text(payload->'securities') code where code=any(:codes))
    """), {"codes": codes}):
        news[(payload["source"], str(payload.get("externalId")))].append((identity, payload))
    for source in session.scalars(select(SourcePayload).where(SourcePayload.api_name.in_(("forecast", "forecast_vip")))):
        body = source.payload
        for entry in body["items"]:
            row = dict(zip(body["fields"], entry, strict=True))
            forecasts[(row["ts_code"], row.get("ann_date"), row.get("end_date"))].append((source.id, row))
    return notices, news, forecasts, pages


def audit(factory, session):
    assert session.scalar(text("show transaction_read_only")) == "on"
    now = session.scalar(text("select current_timestamp"))
    pool = sorted(session.scalars(text(f"""
        select row->>'symbol' from {PUBLISHED_SNAPSHOTS_SQL} published
        cross join lateral json_array_elements(payload->'stocks') row where product='screener'
    """)))
    service = WorkbenchService(StocksDatabaseProvider(SnapshotRepository(factory), factory))
    products = {code: service.get_workbench(code) for code in pool}
    assert pool and len(pool) == len(products)
    charts = {s: p.data["dailyChart"] for s, p in products.items()}
    trading = load_trading_days(session, {s: candle_dates(c["candles"]) for s, c in charts.items()})
    windows = defaultdict(dict)
    for key, state in session.execute(text("select cursor_key,cursor_value from ops.job_cursor where job_name=:job"), {"job": JOB}):
        code, start, end = key.split(":")
        windows[code][(start, end)] = state
    notices, raw_news, raw_forecasts, raw_pages = source_indexes(session, pool)
    core_docs = session.scalars(select(ContentDocument).where(ContentDocument.published_at <= now,
        ContentDocument.copyright_status.in_(DISPLAY_STATUSES)).order_by(ContentDocument.published_at.desc(), ContentDocument.id.desc())).all()
    docs = defaultdict(list)
    for row in core_docs:
        for code in set(row.securities).intersection(pool):
            docs[code].append(row)
    forecasts = defaultdict(list)
    for row in session.scalars(select(FinancialForecast).where(FinancialForecast.ts_code.in_(pool))):
        forecasts[row.ts_code].append(row)
    financial = {model: defaultdict(lambda: defaultdict(list)) for model in (IncomeStatement, FinancialIndicator)}
    for model, index in financial.items():
        for row in session.scalars(select(model).where(model.ts_code.in_(pool))):
            day = max((d for d in (row.ann_date, row.f_ann_date) if d), default=None)
            if day and day >= row.end_date and (model is FinancialIndicator or row.report_type in ("1", "2")):
                index[row.ts_code][row.end_date].append((day, row))
    report = {"checkedAt": now.isoformat(), "universe": len(pool), "documentsBySource": {}, "stocks": {}}
    source_counts = Counter((r.content_type + "/" + r.source) for r in core_docs)
    report["documentsBySource"] = dict(source_counts)
    counts, gaps = Counter(), defaultdict(list)
    schedule_raw = defaultdict(list)
    for source in session.scalars(select(SourcePayload).where(SourcePayload.api_name == "disclosure_date")):
        body = source.payload
        for i, entry in enumerate(body["items"], 1):
            row = dict(zip(body["fields"], entry, strict=True))
            if row["ts_code"] in products:
                schedule_raw[(row["ts_code"], row["end_date"])].append((source.id, i, row))
    schedule = defaultdict(list)
    for r in session.scalars(select(DisclosureSchedule).where(DisclosureSchedule.ts_code.in_(pool))):
        matches = []
        for identity, i, row in schedule_raw[(r.ts_code, r.end_date.strftime("%Y%m%d"))]:
            standard = normalize_disclosure_schedule(row)
            if all(standard[k] == getattr(r,k) for k in ("ann_date","end_date","pre_date","actual_date","modify_date")):
                matches.append([identity, i])
        if not matches:
            gaps["scheduleWithoutExactRaw"].append([r.ts_code,r.id])
        schedule[r.ts_code].append([r.id, r.end_date.isoformat(), r.actual_date.isoformat() if r.actual_date else None, matches])
    report["documentInventory"] = [
        [kind,source,status,count] for kind,source,status,count in session.execute(text("""
            select content_type,source,copyright_status,count(*)
            from core.content_document group by content_type,source,copyright_status order by 1,2,3
        """))]
    report["retainedWorkbenchVersions"] = dict(session.execute(text("""
        select ts_code,count(*) from mart.stock_workbench_snapshot where ts_code=any(:codes) group by ts_code
    """), {"codes": pool}).all())
    for code, product in products.items():
        q, chart, days = product.data["quote"], charts[code], trading[code]
        cutoff = datetime.fromisoformat(q["latestMention"]) if q["latestMention"] else None
        visible = [r for r in docs[code] if cutoff and r.published_at <= cutoff]
        assert len(visible) == q["mentions"] and (visible[0].published_at if visible else None) == cutoff, code
        counts["linkedAfterPublishedLatest"] += len(docs[code]) - len(visible)
        counts["stocksWithMentions"] += bool(visible)
        cd = {"version": product.meta.version, "priceAsOf": product.meta.as_of, "latestMention": q["latestMention"],
              "mentions": len(visible), "documents": [], "windows": [], "forecastIds": [],
              "disclosureSchedule": schedule[code]}
        counts["disclosureScheduleRows"] += len(schedule[code])
        counts["stocksWithDisclosureSchedule"] += bool(schedule[code])
        for row in visible:
            counts["mentions/" + row.content_type] += 1
            ids = []
            if row.source == "cninfo":
                ids = [i for i, r in notices[row.external_id] if r["secCode"] == code.split(".")[0]
                       and r["announcementTitle"] == row.title and row.source_url.endswith("/" + r["adjunctUrl"])
                       and datetime.fromtimestamp(r["announcementTime"] / 1000, ZONE).date() == row.published_at.astimezone(ZONE).date()]
            else:
                ids = [i for i, r in raw_news[(row.source, row.external_id)] if r["title"] == row.title
                       and r["sourceUrl"] == row.source_url and code in r["securities"]
                       and datetime.fromisoformat(r["publishedAt"]) == row.published_at]
            if not ids:
                gaps["documentWithoutExactRaw"].append([code, row.id, row.source, row.external_id])
            cd["documents"].append([row.id, row.external_id, row.content_type, row.source, ids])
        actual = chart["announcements"]
        expected = [r for r in visible if r.content_type == "announcement"]
        assert [e["id"] for e in actual] == [str(r.id) for r in expected], code
        counts["stocksWithNotices"] += bool(actual)
        for e, row in zip(actual, expected, strict=True):
            assert e["title"] == e["label"] == row.title and e["sourceUrl"] == row.source_url
            assert e["source"] == row.source and e["copyrightStatus"] == row.copyright_status
            assert e["securities"] == row.securities and datetime.fromisoformat(e["publishedAt"]) == row.published_at
            if row.copyright_status == "link_only":
                assert "summary" not in e and "body" not in e
            for key, value in announcement_dates(row.published_at, days).items():
                assert e[key] == value, (code, row.id, key)
            counts["placement/" + e["placementStatus"]] += 1
            period = report_period(row.title, q["name"])
            assert bool(period) == ("financialReport" in e), (code, row.id)
            if period:
                cutoff_day = date.fromisoformat(e["disclosedOn"])
                prior = previous_quarter(period)
                details = e["financialReport"]
                assert e["reportPeriod"] == period.isoformat() and details["comparisonPeriod"] == prior.isoformat()
                indexes = [financial[m][code] for m in (IncomeStatement, FinancialIndicator)]
                current, _ = quarter_values(*indexes, period, cutoff_day, exact=True)
                prev, _ = quarter_values(*indexes, prior, cutoff_day, exact=False)
                for key, value in current.items():
                    metric, base = details["metrics"][key], prev[key]
                    assert metric["value"] == (float(value) if value is not None else None)
                    delta = value - base if value is not None and base is not None else None
                    assert metric["changePercent"] == (float(delta / abs(base) * 100) if delta is not None and base else None)
                    if key == "q_roe":
                        assert metric.get("changePoints") == (float(delta) if delta is not None else None)
                    counts["reportCells/" + ("present" if value is not None else "missing")] += 1
                    if value is None:
                        cd.setdefault("emptyReportCells", []).append([row.external_id,key,metric["reason"]])
                counts["financialReportNotices"] += 1
                cd.setdefault("reportSourceIds", {})[row.external_id] = [
                    [r.id for day, r in ix[period] if day == cutoff_day] for ix in indexes]
        coverage = chart.get("announcementCoverage", {})
        window_map = {(w["start"], w["end"]): w for w in coverage.get("windows", [])}
        eligible = {k:v for k,v in windows[code].items() if k[1] >= days.first.isoformat() and k[0] <= days.last.isoformat()}
        assert set(window_map) == set(eligible), code
        completed = set()
        for (a,b), w in window_map.items():
            stored = eligible[(a,b)]
            assert (w["status"], w["count"]) == (stored["status"], stored.get("count")), (code, a)
            counts["windows/" + w["status"]] += 1
            pages = raw_pages.get(f"{code}:{a}:{b}", [])
            if w["status"] == "completed":
                # 重查会追加原件；选每个页码最后一份，截于该轮已查末页。
                latest = {n:(i,r) for i,n,r in sorted(pages)}
                rows = set()
                for n in range(1, len(latest) + 1):
                    _, payload = latest[n]
                    rows.update(r["announcementId"] for r in payload["announcements"])
                    if not payload["hasMore"]:
                        break
                if not latest:
                    gaps["windowWithoutRaw"].append([code,a,b])
                else:
                    assert not payload["hasMore"] and len(rows) == payload["totalAnnouncement"] == w["count"], (code,a,b)
                first, last = max(date.fromisoformat(a), days.first), min(date.fromisoformat(b), days.last)
                completed.update(first + timedelta(days=d) for d in range((last-first).days+1))
                counts["checkedEmptyWindows"] += w["count"] == 0
            cd["windows"].append([a,b,w["status"],w["count"],[i for i,_,_ in pages]])
        cd["chartBounds"] = [days.first.isoformat(), days.last.isoformat()]
        cd["checkedCalendarDays"] = len(completed)
        cd["inScopeCalendarDays"] = (days.last-days.first).days+1 if code.endswith((".SH",".SZ")) else 0
        counts["checkedCalendarDays"] += len(completed)
        counts["inScopeCalendarDays"] += cd["inScopeCalendarDays"]
        counts["stocksWithCheckedWindows"] += bool(completed)
        by_id = {e["id"]:e for e in chart.get("forecastEvents", [])}
        placed = unplaced = 0
        match_keys = Counter((e.get("disclosedOn"),e.get("reportPeriod")) for e in actual if "业绩预告" in e["label"] and e.get("reportPeriod"))
        forecast_keys = Counter((e["disclosedOn"], e["reportPeriod"]) for e in by_id.values())
        counts["uniquelyMatchedForecastNotices"] += sum(v == forecast_keys[k] == 1 for k,v in match_keys.items())
        for r in forecasts[code]:
            fk = (code, r.ann_date.strftime("%Y%m%d") if r.ann_date else None, r.end_date.strftime("%Y%m%d"))
            sources = [i for i,raw in raw_forecasts[fk] if all(raw.get(k) == r.data.get(k) for k in FORECAST)]
            if not sources:
                gaps["forecastWithoutExactRaw"].append([code,r.id])
            if not r.ann_date or not days.first <= r.ann_date <= days.last:
                counts["forecastOutsideChart"] += 1
                cd["forecastIds"].append([r.id, "outside", sources])
                continue
            event_day, _, _ = days.locate(r.ann_date)
            if event_day is None:
                unplaced += 1
                cd["forecastIds"].append([r.id, "pending", sources])
                continue
            e = by_id["forecast-" + str(r.id)]
            assert (e["date"], e["disclosedOn"], e["reportPeriod"]) == (event_day.isoformat(), r.ann_date.isoformat(), r.end_date.isoformat())
            assert e["forecast"] == {k:r.data.get(k) for k in FORECAST}
            cd["forecastIds"].append([r.id, "placed", sources])
            placed += 1
        assert len(by_id) == placed and unplaced == chart.get("unplacedForecastCount", 0), code
        counts["placedForecasts"] += placed
        counts["unplacedForecasts"] += unplaced
        report["stocks"][code] = cd
    report["summary"] = dict(counts)
    report["gaps"] = dict(gaps)
    return report


if __name__ == "__main__":
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        print(json.dumps(audit(factory, session), indent=2, ensure_ascii=True))
