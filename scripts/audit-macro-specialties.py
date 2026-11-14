"""逐组重放已发布的宏观专题与当前可追溯企业报表；只读、不触发供应商。"""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.enterprise_contract import enterprise_loader, valid_enterprise_analysis
from app.macro.extensions.view import load_extension_inputs
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.shared.readonly_database import open_readonly_factory


def compare_series(before, after):
    assert before["id"] == after["id"]
    rows = {p["period"]: p for p in before["points"]}
    rebuilt = {p["period"]: p for p in after["points"]}
    keys = set(rows) | set(rebuilt)
    changed = sorted(p for p in keys if rows.get(p) != rebuilt.get(p))
    quality = {p["period"]:p for p in before["quality"]}
    new_quality = {p["period"]:p for p in after["quality"]}
    mismatch = {}
    for day in sorted(set(quality) | set(new_quality)):
        a, b = quality.get(day,{}), new_quality.get(day,{})
        fields = sorted(k for k in a.keys()|b.keys() if a.get(k) != b.get(k))
        if fields:
            mismatch[day] = fields
    last = before["quality"][-1]
    return {"publishedPoints":len(rows), "rebuiltPoints":len(rebuilt), "equalPoints":len(keys)-len(changed),
            "changedPoints":changed, "changedExamples":[{"published":rows.get(p),"rebuilt":rebuilt.get(p)} for p in changed[:1]],
            "publishedQuality":len(quality),"equalQuality":len(set(quality)|set(new_quality))-len(mismatch),
            "changedQualityFields":dict(Counter(k for fields in mismatch.values() for k in fields)),
            "lastPublished":max(rows, default=None), "latestReview":{k:last.get(k) for k in
            ("period","status","candidateCount","sampleCount","coverage","valuationStatus") if k in last},
            "latestExclusions":dict(Counter(last.get("excluded",{}).values()))}


def compare_set(before, after):
    assert [c["id"] for c in before] == [c["id"] for c in after]
    return {c["id"]:compare_series(c,d) for c,d in zip(before,after,strict=True)}


def document(doc):
    return {k:doc[k] for k in ("documentId","runId","rawSha256","retrievedAt","year","url") if k in doc} | {
        k:v for k,v in (("url",doc.get("request",{}).get("url")),("rawSha256",doc.get("normalized",{}).get("rawSha256")),
        ("businessSha256",doc.get("businessSha256")),("coreCount",len(doc.get("coreBindings",[])))) if v is not None}


def summarize_extension(ext):
    groups = {k:{"id":s["id"],"observations":len(s["points"]),"status":s["status"],"reason":s["reason"],
                 "retrievedAt":s["retrievedAt"],"fingerprint":s["evidenceFingerprint"]}
              for k,s in ext["sources"].items()}
    fiscal = ext["fiscal"]
    groups["governmentBonds"] = {"requestedMonths":len(fiscal["points"]),"missingMonths":len(fiscal["missingPeriods"]),
        "latest":fiscal["latest"],"expectedDocuments":len(fiscal["window"]["expectedDocuments"]),
        "documents":[document(d) for d in fiscal["documents"]],"latestAttempt":fiscal["latestAttempt"]}
    for k,g in ext["official"]["groups"].items():
        bundles = [g,*g.get("history",[])]
        docs = {d["runId"]:document(d) for b in bundles for d in b["documents"]}
        groups[k] = {"series":{s["id"]:{"values":sum(p["value"] is not None for p in s["points"]),
            "missingPeriods":len(s["missingPeriods"]),"latest":s["latest"]} for s in g["series"]},
            "documents":docs, "latestAttempt":{f:g["latestAttempt"].get(f) for f in ("status","errorCode")},
            "progress":g["progress"]}
    market = ext["marketSummary"]
    for k,g in market["groups"].items():
        groups[k]={"targets":[t["plan"]["targetDate"] for t in g["targets"]],
                   "documents":[document(d) for t in g["targets"] for d in t["documents"]],
                   "monthlyReasons":dict(Counter(r.get("reason") or "available" for r in market["monthly"]))}
    return {"sourceHash":ext["sourceHash"], "groups":groups,"marketLatest":[r for r in market["monthly"] if r["value"] is not None],
            "nominalRealGdp":{k:sum(p.get(k) is not None for p in ext["nominalGdp"]) for k in ("amount","nominalYoy","realYoy","approxDeflator","strictDeflator")}}


def inspect(factory, session):
    assert session.scalar(text("show transaction_read_only")) == "on"
    version, at, extension, enterprise = session.execute(text("""
        select version,as_of,payload->'extensionInsights',payload->'enterpriseAnalysis'
        from mart.published_snapshot where product='macro-transmission'
    """)).one()
    insights = session.execute(text("select version, payload->'extensionInsights' from mart.published_snapshot where product='macro-insights'")).one()
    assert insights == (version, extension)
    catalog=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    builder=MacroMartBuilder(MacroRepository(factory),catalog=catalog,extension_loader=lambda day:load_extension_inputs(factory,day))
    rebuilt=builder.load_extension_insights(at,builder.load_observations(catalog,at))
    assert rebuilt == extension
    calc=enterprise_loader(factory,settings)(at)
    assert valid_enterprise_analysis(enterprise) and valid_enterprise_analysis(calc)
    assert enterprise["symbols"] == calc["symbols"] and enterprise["policy"] == calc["policy"]
    originals=enterprise["officialTargets"]["targets"]
    refreshed={t["code"]:t for t in calc["officialTargets"]["targets"]}
    official = {}
    for t in originals:
        c = refreshed[t["code"]]
        w = t["witness"]
        assert (t["witness"],t["sourceState"],t["status"],t["poolMembers"]) == (c["witness"],c["sourceState"],c["status"],c["poolMembers"])
        official[t["code"]] = {"label":t["label"],"status":t["status"],"sourceState":t["sourceState"],
            "memberCount":w["memberCount"] if w else None,"membersAsOf":w["membershipDate"] if w else None,
            "documents":w["documents"] if w else [], "poolMembers":t["poolMembers"],
            "analysisExact":t["analysis"] == c["analysis"],
            "cards":compare_set(t["analysis"]["cards"],c["analysis"]["cards"]) if t["analysis"] else {}}
    groups={g["code"]:g for g in calc["industryComparison"]["groups"]}
    industries={}
    for g in enterprise["industryComparison"]["groups"]:
        h=groups[g["code"]]
        assert g["symbols"] == h["symbols"] and g["name"] == h["name"]
        industries[g["code"]]={"name":g["name"],"members":g["symbols"],"analysisExact":g["analysis"] == h["analysis"],
            "cards":compare_set(g["analysis"]["cards"],h["analysis"]["cards"])}
    return {"checkedAt":datetime.now(timezone.utc).isoformat(),"asOf":at.isoformat(),"version":version,
            "extension":summarize_extension(extension),"enterprise":{
                "publishedInputHash":enterprise["inputHash"],"rebuiltInputHash":calc["inputHash"],
                "symbols":enterprise["symbols"],"policy":enterprise["policy"],"basis":enterprise["basis"],
                "cards":compare_set(enterprise["cards"],calc["cards"]),
                "ledger":compare_set(enterprise["ledger"]["metrics"],calc["ledger"]["metrics"]),
                "rea":compare_set(enterprise["rea"]["series"],calc["rea"]["series"]),
                "industryComparison":industries,"officialTargets":official}}


if __name__ == "__main__":
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        report = inspect(factory,session)
    print(json.dumps(report, ensure_ascii=True, indent=2))
