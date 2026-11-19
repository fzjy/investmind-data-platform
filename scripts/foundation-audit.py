"""只读核对本地底座的目录映射、采集凭证和存储。"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "packages/news-collector/src")]
from app.config import settings
from app.shared.readonly_database import open_readonly_factory
from app.shared.snapshot_sql import PUBLISHED_SNAPSHOTS_SQL

OUT = ROOT / "docs/foundation-audit"
SCHEMAS = ("raw", "core", "derived", "mart", "ops", "public")


def read_json(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def save(name, obj):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(
        json.dumps(obj, ensure_ascii=False, default=str, indent=2) + "\n", encoding="utf-8"
    )


def rows(s, sql, **params):
    return list(map(dict, s.execute(text(sql), params).mappings()))


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode("utf-8").strip("\r\n")


def base():
    status = git("status", "--porcelain=v1", "--untracked-files=all").splitlines()
    changes = {}
    for line in status:
        filename = line[3:]
        file = ROOT / filename
        if file.is_file() and "foundation-audit" not in filename:
            changes[filename] = {"status": line[:2], "sha256": sha256(file.read_bytes()).hexdigest()}
    return {
        "commit": git("rev-parse", "HEAD"),
        "changes": changes,
        "providerMode": settings.tushare_mode,
        "schedulerTimezone": settings.sync_timezone,
    }


def main():
    url = make_url(settings.database_read_url)
    if not (OUT / "baseline.json").exists():
        save("baseline.json", base())
    if (
        url.host not in ("localhost", "127.0.0.1", "::1")
        or url.query
        or url.drivername != "postgresql+psycopg"
    ):
        raise RuntimeError("需要本机 PostgreSQL")
    with open_readonly_factory(settings.database_read_url) as factory, factory() as s:
        assert s.scalar(text("show transaction_read_only")) == "on"
        assert s.scalar(text("show transaction_isolation")) == "repeatable read"
        sys.path.insert(0, str(ROOT / "scripts"))
        from foundation_audit_dividends import dividend_report
        from foundation_audit_storage import cross_checks, storage

        checked = rows(
            s,
            "select now() as checked_at,pg_current_snapshot()::text as snapshot,current_setting('TimeZone') as database_timezone, (select version_num from public.alembic_version) as migration",
        )[0]
        from foundation_audit_inventory import inventory

        db = {
            "transaction": checked,
            "relations": storage(s, rows, SCHEMAS),
            "comparisons": cross_checks(s, rows, PUBLISHED_SNAPSHOTS_SQL),
        }
        catalog = [
            item
            for filename in read_json("docs/data-catalog.json")["files"]
            for item in read_json("docs/" + filename)
        ]
        from foundation_audit_ranges import summarize
        from foundation_audit_products import catalog_examples, freeze

        db["ranges"] = summarize(s)
        db["productResponses"] = freeze(factory, read_json)
        db["catalogExamples"] = catalog_examples(factory, catalog)
        entries = inventory(s, ROOT, catalog, db, read_json)
        catalog_files = [
            "docs/data-catalog.json",
            *["docs/" + filename for filename in read_json("docs/data-catalog.json")["files"]],
        ]
        entries["catalogFileSha256"] = {
            name: sha256((ROOT / name).read_bytes()).hexdigest() for name in catalog_files
        }
        save("inventory.json", entries)

        result, event_evidence = dividend_report(
            s, read_json("config/portfolio/portfolio.json")["routine"]["dividendRevisionDays"]
        )
        from foundation_audit_storage import request_groups

        db["overlappingRequestGroups"] = request_groups(s, rows, result)
        save("database.json", db)
        save("dividends.json", result)
        with (OUT / result["events"]["variantsFile"]).open("w", encoding="utf8", newline="\n") as file:
            for item in event_evidence:
                file.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        from foundation_audit_report import dividend_doc, inventory_doc

        inventory_doc()
        dividend_doc()
        print(
            json.dumps(
                {
                    "domainItems": entries["byDomain"],
                    "relations": len(db["relations"]),
                    "dividends": result["summary"],
                },
                ensure_ascii=True,
            )
        )


if __name__ == "__main__":
    main()
