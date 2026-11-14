from datetime import date, datetime, timezone
import json, sys
from pathlib import Path
from sqlalchemy import text, select

p = Path(__file__).resolve().parents[1]

sys.path[:0] = [str(p / "backend"), str(p / "packages/news-collector/src")]
from app.db.models import MacroObservation
from app.macro.import_lineage import verify_import_lineage, is_verified_import
from app.config import Settings
from app.shared.readonly_database import open_readonly_factory
from app.connectors.external_files.contracts import (
    ContentImportRecord,
    MacroImportRecord,
)


def load(relative, root=p):
    return json.loads((root / relative).read_text(encoding="utf-8-sig"))


def write(obj):
    sys.stdout.buffer.write(
        (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )


c = load("backend/config/macro-series.json")
e = load("backend/config/macro-extensions.json")
orig = load("docs/research/investmind-macro-supply.json")
sp = load("docs/research/investmind-macro-specialties.json")
assert {row["id"] for row in c["series"]} == set(orig["series"])
with open_readonly_factory(
    Settings.from_env().database_read_url
) as factory, factory() as session:
    assert session.scalar(text("SHOW transaction_read_only")) == "on"
    snapshot = {
        k: (v, d)
        for k, v, d in session.execute(
            text(
                "SELECT product, version, payload FROM mart.published_snapshot WHERE product IN ('macro-transmission','macro-insights')"
            )
        )
    }
    assert all(v == sp["version"] for v, d in snapshot.values())
    ins = snapshot["macro-insights"][1]
    files = [
        dict(r)
        for r in session.execute(
            text(
                "SELECT status,count(*) AS files,sum(valid_count) AS valid_rows,sum(rejected_count) AS rejected_rows FROM raw.external_file GROUP BY status ORDER BY status"
            )
        ).mappings()
    ]
    records = [
        dict(r)
        for r in session.execute(
            text(
                "SELECT f.status,r.record_type,r.payload->>'source' AS source,count(*) AS rows FROM raw.external_record r JOIN raw.external_file f ON f.id=r.external_file_id GROUP BY 1,2,3 ORDER BY 1,2,3"
            )
        ).mappings()
    ]
    docs = [
        dict(r)
        for r in session.execute(
            text(
                "SELECT content_type,source,copyright_status,count(*) AS rows,count(summary) AS summaries,count(body) AS bodies FROM core.content_document GROUP BY 1,2,3 ORDER BY 1,2,3"
            )
        ).mappings()
    ]
    imports = []
    imported = session.execute(
        text(
            "SELECT id,payload FROM raw.external_record WHERE record_type='macro' ORDER BY id"
        )
    ).all()
    cutoff = datetime.fromisoformat(sp["asOf"])
    for record_id, record in imported:
        item = next(r for r in c["series"] if r["id"] == record["seriesId"])
        providers = [
            src["provider"]
            for src in sorted(item["sources"], key=lambda s: s["priority"])
        ]
        obs = session.scalars(
            select(MacroObservation).where(
                MacroObservation.series_id == item["id"],
                MacroObservation.observed_at
                == date.fromisoformat(record["observedAt"]),
                MacroObservation.released_at <= cutoff,
            )
        ).all()
        verify_import_lineage(session, obs)
        latest = {}
        for o in sorted(obs, key=lambda o: (o.released_at, o.revision)):
            if o.source in providers or is_verified_import(o):
                latest[o.source] = o
        candidates = [
            o
            for o in latest.values()
            if o.valid_until is None or o.valid_until > cutoff
        ]
        selected = (
            min(
                candidates,
                key=lambda o: (
                    (
                        providers.index(o.source)
                        if o.source in providers
                        else len(providers)
                    ),
                    o.source,
                ),
            )
            if candidates
            else None
        )
        series = next(s for s in ins["sandboxIndicators"] if s["id"] == item["id"])
        points = [
            q["value"] for q in series["values"] if q["month"] == record["observedAt"]
        ]
        assert (selected is None and not points) or (
            len(points) == 1 and abs(points[0] - float(selected.value)) < 1e-9
        )
        imports.append(
            {
                "recordId": record_id,
                "seriesId": item["id"],
                "source": record["source"],
                "observedAt": record["observedAt"],
                "releasedAt": record["releasedAt"],
                "revision": record["revision"],
                "importedValue": record["value"],
                "sourceUrl": record["sourceUrl"],
                "selectedSource": selected.source if selected else None,
                "selectedCoreId": selected.id if selected else None,
                "publishedValue": points[0] if points else None,
                "selectedImportedRecord": bool(
                    selected
                    and selected.source == record["source"]
                    and selected.revision == record["revision"]
                    and selected.released_at
                    == datetime.fromisoformat(record["releasedAt"])
                ),
            }
        )


def paths(obj, needle, parent="", hits=None):
    hits = [] if hits is None else hits
    if isinstance(obj, dict):
        for k, v in obj.items():
            pk = parent + "." + str(k) if parent else str(k)
            if k == needle:
                hits.append(parent + ".{seriesId}")
            if isinstance(v, (dict, list)) or v == needle:
                paths(v, needle, pk, hits)
    elif isinstance(obj, list):
        for value in obj:
            if isinstance(value, (dict, list)) or value == needle:
                paths(value, needle, parent + "[]", hits)
    elif obj == needle:
        hits.append(parent)
    return hits


basic = {}
for series in c["series"]:
    key = series["id"]
    audit = orig["series"][key]
    sources = [
        {
            k: s.get(k)
            for k in (
                "priority",
                "provider",
                "api",
                "series",
                "field",
                "responseFields",
                "transform",
                "sourceUrl",
            )
        }
        for s in series["sources"]
    ]
    entry = {
        "name": series["name"],
        "frequency": series["frequency"],
        "unit": series["unit"],
        "sources": sources,
        "publishedSource": audit["status"]["actualSource"],
        "periods": audit["periodCount"],
        "collection": audit["currentAudit"]["status"],
        "sourceError": audit["currentAudit"]["errorCode"],
        "derivedConsumers": [
            k for k, v in orig["derived"].items() if key in v["inputs"]
        ],
    }
    # selector occurrences only, no numeric coincidence; omit large source audit mix-ins
    entry["publishedFieldPaths"] = {
        k: sorted(set(paths(data, key))) for k, (_, data) in snapshot.items()
    }
    basic[key] = entry
targets = {}
for k, t in sp["enterprise"]["officialTargets"].items():
    targets[k] = {
        "name": t["label"],
        "sourceStatus": t["sourceState"]["status"],
        "memberDate": t["membersAsOf"],
        "members": t["memberCount"],
        "membersInPool": len(t["poolMembers"]),
        "sourceDocuments": [
            {"kind": d["kind"], "url": d["url"], "sha256": d["rawSha256"]}
            for d in t["documents"]
        ],
    }
out = {
    "checkedAt": datetime.now(timezone.utc).isoformat(),
    "version": sp["version"],
    "readOnly": True,
    "sourceRequests": 0,
    "basicSeries": basic,
    "extensions": {
        k: {
            "enabled": g["collectionEnabled"],
            "schedule": g["schedule"],
            "sourceGroupEvidence": k,
        }
        for k, g in e["groups"].items()
    },
    "officialTargets": targets,
    "externalImport": {
        "schemaFields": {
            k: [v.alias or key for key, v in m.model_fields.items()]
            for k, m in [("macro", MacroImportRecord), ("content", ContentImportRecord)]
        },
        "files": files,
        "rows": records,
        "macroRowsAtPublication": imports,
    },
    "storedContent": docs,
}
write(out)
