"""核对两项宏观产品的新旧路由、历史版本及逐系列核心观察与原件。"""

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from math import isclose
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from app.config import settings
from app.db.models import MacroObservation, PublishedSnapshot, SourcePayload
from app.macro.audit import CollectionAuditReader
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.database_provider import MacroDatabaseProvider
from app.macro.import_lineage import is_verified_import, verify_import_lineage
from app.macro.routes import create_router
from app.shared.errors import DataServiceError
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository


def raw_point(source, data, period, vintage):
    field = source.field
    if source.provider == "tushare":
        rows = list(data.get("rows", []))
        for page in data.get("pages", []):
            body = page["response"]
            rows.extend(dict(zip(body["fields"], row, strict=True)) for row in body["items"])
        for r in rows:
            stamp = r.get("month") or r.get("quarter") or r.get("trade_date")
            if stamp in {period.strftime("%Y%m"), period.strftime("%Y%m%d"), f"{period.year}Q{(period.month-1)//3+1}"}:
                return Decimal(str(r[field]))
    if source.provider == "nbs":
        for r in data.get("releases", []):
            if r["observedAt"] == str(period):
                return Decimal(str(r["value"]))
    if source.provider == "chinamoney":
        for page in data.get("pages", []):
            for r in page["response"]["records"]:
                if r["dateString"] == str(period):
                    return Decimal(r["tenRate"])
    if source.provider == "fred":
        observations = data.get("observations", [])
        def level(on_date):
            candidates = [Decimal(r["value"]) for r in observations if r["date"] == str(on_date)
                          and r["realtime_start"] <= str(vintage) <= r["realtime_end"] and r["value"] != "."]
            return candidates[0] if len(set(candidates)) == 1 else None
        value = level(period)
        if value is None or source.transform == "identity":
            return value
        if source.transform in ("pc1", "chg"):
            shift = 12 if source.transform == "pc1" else 1
            index = period.year*12+period.month-1-shift
            prior = level(date(index//12, index%12+1, 1))
            if prior is not None:
                if source.transform == "pc1":
                    return (value/prior-1)*100 if prior else None
                return value-prior
    return None


def latest_raw_samples(session, catalog, current, cutoff):
    ids = session.execute(text("""
        select distinct on (b.dataset,b.source) b.dataset,b.source,r.id from raw.source_payload r
        join raw.ingestion_batch b on b.id=r.batch_id
        where b.status='completed' and b.dataset=any(:datasets) and r.captured_at<=:cutoff
          and r.payload->>'status'='success'
        order by b.dataset,b.source,r.captured_at desc,r.id desc
    """), {"datasets": ["macro:"+d.id for d in catalog.series], "cutoff": cutoff})
    raw_ids = {(n.removeprefix("macro:"), s): i for n,s,i in ids}
    samples = {}
    for definition in catalog.series:
        row = current.get(definition.id)
        if not row:
            continue
        source = next((s for s in definition.sources if s.provider==row.source), None)
        raw_id = raw_ids.get((definition.id, row.source))
        if not raw_id or not source:
            samples[definition.id] = {"status": "no-matching-batch"}
            continue
        raw = session.get(SourcePayload, raw_id)
        payload = raw.payload
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        assert sha256(encoded).hexdigest() == raw.content_hash
        window = payload["request"]["window"]
        # FRED 全历史某一 vintage 的可见期不跨越本次原件实际截取的时点。
        sample_day = min(cutoff.date(), datetime.fromisoformat(window["asOf"]).date())
        reconstructed = raw_point(source, payload["data"], row.observed_at, sample_day)
        if reconstructed is not None:
            assert isclose(float(reconstructed), float(row.value), rel_tol=1e-8, abs_tol=1e-8), (definition.id, reconstructed, row.value)
        samples[definition.id] = dict(rawId=raw.id, batchId=raw.batch_id, capturedAt=raw.captured_at.isoformat(),
            requestWindow=window, transform=source.transform, reconstructed=reconstructed,
            sourceField=source.field, status="raw-equal" if reconstructed is not None else "value-unverified")
    return samples


def capture_routes(factory, session):
    app = FastAPI()
    # 固定本次读取时刻使实时审计 readAt 在两个别名间也可严格比较。
    checked_at = datetime.now(timezone.utc)
    reader = CollectionAuditReader(factory, clock=lambda: checked_at)
    app.include_router(create_router(provider=MacroDatabaseProvider(SnapshotRepository(factory)), collection_audit=reader))
    @app.exception_handler(DataServiceError)
    async def domain_error(_request, error):
        return JSONResponse({"code":error.code}, status_code=error.status)
    products, history, evidence, latest_audit = {}, {}, {}, None
    for group in ("transmission", "insights"):
        name = f"macro-{group}"
        product = session.get(PublishedSnapshot, name)
        products[group] = product
        version_rows = session.execute(text("""
            select snapshot_version,as_of from mart.page_snapshot where product=:name order by as_of,id
        """), {"name": name}).all()
        history[group] = {v: t.isoformat() for v,t in version_rows}
        evidence[group] = {"pointerVersion": product.version, "asOf": product.as_of.isoformat(),
                           "historyVersions": len(version_rows), "historyBusinessInstants": len(set(history[group].values())),
                           "historyRange": [version_rows[0][1].isoformat(),version_rows[-1][1].isoformat()]}
    assert history["transmission"] == history["insights"]
    assert products["transmission"].version == products["insights"].version
    sampled_old = next(iter(history["transmission"]))
    with TestClient(app) as client:
        for group, product in products.items():
            queries = [{}, {"version": product.version}, {"version": sampled_old}, {"version": "no-such-audit-version"},
                       {"version": ""}]
            if group == "transmission":
                queries += [{"view":"initial"}, {"view":"initial", "version":product.version}, {"view":"invalid"}]
            modes = []
            for params in queries:
                a,b = [client.get(path, params=params) for path in (f"/api/v1/macro/{group}", f"/api/data/macro-{group}")]
                assert a.status_code == b.status_code and a.content==b.content
                expected = 404 if params.get("version")=="no-such-audit-version" else 422 if (
                    params.get("version") == "" or params.get("view")=="invalid") else 200
                assert a.status_code == expected
                modes.append({"params": params, "status": expected})
                if not a.is_success:
                    continue
                obj = a.json()
                version = params.get("version", product.version)
                assert obj["meta"]["version"] == version
                if params.get("view") == "initial":
                    base = product.payload.get("enterpriseAnalysis", {})
                    assert obj["data"]["enterpriseAnalysis"] == {k:v for k,v in base.items() if k not in ("industryComparison","officialTargets")}
                elif version == product.version:
                    assert obj["data"] == product.payload
                if group=="insights":
                    assert a.headers["cache-control"] == b.headers["cache-control"] == "no-store"
                    assert obj["collectionAudit"]["basis"] == "current-collection-records"
                    latest_audit = obj["collectionAudit"]
            evidence[group]["routeCases"] = modes
    return products, evidence, latest_audit


def inspect(factory, session):
    products, routes, audit = capture_routes(factory, session)
    insights, transmission = products["insights"].payload, products["transmission"].payload
    cutoff = products["insights"].as_of
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    definitions = {d.id: d for d in catalog.series}
    observations = list(session.scalars(select(MacroObservation).where(MacroObservation.series_id.in_(definitions),
                             MacroObservation.released_at <= cutoff, MacroObservation.observed_at <= cutoff.date())))
    verify_import_lineage(session, observations)
    all_rows = defaultdict(list)
    for r in observations:
        all_rows[r.series_id].append(r)
    published = {r["id"]: r for r in insights["sandboxIndicators"]}
    trends = {r["id"]:r for r in insights["trendDefinitions"]}
    assert transmission["seriesStatus"] == insights["seriesStatus"]
    assert set(published) == set(trends) == set(definitions)
    states = {r["id"]: r for r in insights["seriesStatus"]}
    current_audit = {r["id"]: r for r in audit["entries"]}
    evidence, all_series, newest = {}, {}, {}
    for key, definition in definitions.items():
        by_source = defaultdict(list)
        providers = [s.provider for s in sorted(definition.sources,key=lambda s:s.priority)]
        for row in all_rows[key]:
            if row.source in providers or is_verified_import(row):
                by_source[row.observed_at,row.source].append(row)
        periods = defaultdict(list)
        for (period,_), revisions in by_source.items():
            last = max(revisions, key=lambda r:(r.released_at,r.revision))
            if last.valid_until is None or last.valid_until > cutoff:
                periods[period].append(last)
        selected = {period:min(choices,key=lambda r: (providers.index(r.source) if r.source in providers else len(providers),r.source))
                    for period,choices in sorted(periods.items())}
        core = {p.isoformat():r.value for p,r in selected.items()}
        all_series[key] = core
        points = published[key]["values"]
        assert points == trends[key]["series"][0]["values"]
        assert [p["month"] for p in points] == list(core)
        assert all(isclose(float(core[p["month"]]),p["value"],rel_tol=1e-10,abs_tol=1e-10) for p in points)
        last = selected[max(selected)] if selected else None
        status = states[key]
        if last:
            newest[key] = last
        assert status["observedAt"] == (str(last.observed_at) if last else None)
        assert status["actualSource"] == (last.source if last else None)
        repeated = [items for items in by_source.values() if len(items)>1]
        changed = [items for items in repeated if len({v.value for v in items})>1]
        sample_revision = sorted(changed[0], key=lambda r:(r.released_at,r.id)) if changed else []
        evidence[key] = dict(name=definition.name, frequency=definition.frequency, unit=definition.unit,
            configuredSources=[{"provider":s.provider,"locator":s.api or s.series,"field":s.field,"transform":s.transform}
                               for s in definition.sources], status=status, currentAudit=current_audit[key],
            periodCount=len(points), firstPeriod=min(core,default=None), latestPeriod=max(core,default=None),
            latest=last.value if last else None, latestCoreId=last.id if last else None,
            sameSourceMultiVersionPeriods=len(repeated), sameSourceValueRevisions=len(changed),
            exampleRevision=[{"id":r.id,"period":str(r.observed_at),"value":r.value,"source":r.source,
                              "releasedAt":r.released_at.isoformat(),"revision":r.revision} for r in sample_revision])
    raw_samples = latest_raw_samples(session,catalog,newest,cutoff)
    for key, sample in raw_samples.items():
        evidence[key]["latestRawSample"] = sample
    derived = {}
    for item in insights["derivedInsights"]["series"]:
        left,right = [all_series[k] for k in item["inputIds"]]
        periods = sorted(set(left)&set(right))
        assert [p["month"] for p in item["points"]] == periods
        scale = 100 if item["unit"]=="bp" else 1
        assert all(isclose(float((left[p["month"]]-right[p["month"]])*scale),p["value"],rel_tol=1e-9,abs_tol=1e-8)
                   for p in item["points"])
        derived[item["id"]] = {"inputs":item["inputIds"],"unit":item["unit"],"points":len(periods),
                               "latest":item["latestPeriod"],"status":item["status"],"reason":item["reason"]}
    assert sorted(set().union(*(s.keys() for s in all_series.values()))) == insights["sandboxMonths"]
    extension = insights["extensionInsights"]
    assert transmission["extensionInsights"] == extension
    fiscal = extension["fiscal"]
    enterprise = transmission["enterpriseAnalysis"]
    jobs = session.execute(text("""
        select job_name, status, details->>'trigger' trigger, count(*) runs, min(started_at) first, max(started_at) last
        from ops.job_run where job_name in ('backfill-macro-history','catchup-macro','sync-macro') group by 1,2,3 order by 1,2,3
    """)).mappings()
    return {"catalogVersion":catalog.catalog_version, "overlap":catalog.defaults.overlap.model_dump(by_alias=True),
            "schedule":{"enabled":settings.macro_sync_enabled,"weekday":settings.macro_sync_day_of_week,
                        "time":str(settings.macro_sync_time),"zone":settings.sync_timezone,
                        "catchupOnStart":settings.macro_sync_catchup_on_start, "historyYears":settings.macro_history_years},
            "routes":routes, "transmissionKeys":sorted(transmission),"insightsKeys":sorted(insights),
            "series":evidence, "datesInUnion":len(insights["sandboxMonths"]), "derived":derived,
            "collectionAudit":{"entries":len(current_audit),"statuses":Counter(v["status"] for v in current_audit.values()),
                               "basis":audit["basis"]},"jobLog":[dict(r) for r in jobs],
            "extensions":{"basis":extension["basis"],"realGdp":len(extension["realGdp"]),"nominalGdp":len(extension["nominalGdp"]),
                          "vixPeriod":extension["vix"]["period"],"fxPeriod":extension["fx"]["period"],
                          "fiscal":{"status":fiscal["status"],"points":len(fiscal["points"]),"missingPeriods":len(fiscal["missingPeriods"]),
                                    "documents":len(fiscal["documents"]),"latest":fiscal["latest"]},
                          "officialGroups":sorted(extension["official"]["groups"]),
                          "marketSummaryGroups":sorted(extension["marketSummary"]["groups"])},
            "enterprise":{"poolCount":enterprise["poolCount"],"basis":enterprise["basis"],"version":enterprise["algorithmVersion"],
                          "asOfDate":enterprise["asOfDate"],"poolVersion":enterprise["poolVersion"],"cards":len(enterprise["cards"])}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        evidence = inspect(factory, session)
    args.output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2,default=str)+"\n",encoding="utf-8")


if __name__ == "__main__":
    main()
