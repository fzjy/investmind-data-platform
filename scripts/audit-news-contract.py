"""只读遍历市场快讯游标，逐条比对可交付原文与权限，并检查市场快照快讯的重叠。"""
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings
from app.content.routes import create_router
from app.db.models import ContentDocument
from app.shared.readonly_database import open_readonly_factory


def main():
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        at = datetime.now(timezone.utc)
        source = session.scalars(select(ContentDocument).where(
            ContentDocument.content_type == "news", ContentDocument.published_at <= at,
            ContentDocument.copyright_status.in_(("public_domain", "licensed", "link_only"))
        ).order_by(ContentDocument.published_at.desc(), ContentDocument.id.desc())).all()
        snapshot = session.scalar(text("select payload->'marketNews' from mart.published_snapshot where product='market-explorer'"))
        api = FastAPI()
        api.include_router(create_router(factory))
        ids, cursors = [], set()
        pages = 0
        fields = Counter()
        with TestClient(api) as client:
            first = client.get("/api/v1/news").json()
            old = client.get("/api/data/market-news").json()
            assert old == first and client.get("/api/v1/news?cursor=invalid").status_code == 400
            assert client.get("/api/v1/news?limit=51").status_code == 422
            assert client.post("/api/v1/news", json={}).status_code == 405
            cursor = None
            while True:
                params = {"limit": 50}
                if cursor:
                    params["cursor"] = cursor
                response = client.get("/api/v1/news", params=params)
                response.raise_for_status()
                page = response.json()
                pages += 1
                for item in page["items"]:
                    row = source[len(ids)]
                    assert item["id"] == str(row.id)
                    assert item["publishedAt"] == row.published_at.isoformat()
                    expected_source = "新浪财经" if row.source == "sina-finance" else row.source
                    assert item["source"] == expected_source
                    assert item["time"] == row.published_at.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M")
                    assert item["market"] == ("A股" if row.securities else "市场")
                    for key, val in (("sourceUrl",row.source_url),("title",row.title),("securities",row.securities),
                                     ("contentType",row.content_type),("copyrightStatus",row.copyright_status)):
                        assert item[key] == val
                    if row.copyright_status == "link_only":
                        assert "body" not in item and "summary" not in item
                    else:
                        assert item["summary"] == row.summary and item["body"] == row.body
                    fields[row.copyright_status] += 1
                    ids.append(item["id"])
                cursor = page["nextCursor"]
                if cursor is None:
                    break
                assert cursor not in cursors and page["items"]
                cursors.add(cursor)
        assert len(ids) == len(set(ids)) == len(source)
        first_ids = [r["id"] for r in first["items"]]
        assert first_ids == ids[:20]
        snapshot_ids = {r["id"] for r in snapshot}
        report = {"checkedAt":at.isoformat(),"visible":len(ids),"pagesOf50":pages,
                  "firstPage":len(first_ids),"sameNewsIdsInMarketSnapshot":len(snapshot_ids.intersection(first_ids)),
                  "snapshotEntries":len(snapshot),"copyright":fields,"health":first["health"],
                  "oldest":source[-1].published_at.isoformat() if source else None,
                  "newest":source[0].published_at.isoformat() if source else None,
                  "orderedIdsSha256":hashlib.sha256(",".join(ids).encode()).hexdigest()}
    print(json.dumps(report, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
