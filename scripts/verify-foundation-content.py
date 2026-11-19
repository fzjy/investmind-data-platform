"""在同一个只读库存视图，逐页对照公共内容、独立 SQL 与新闻产品。"""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from app.config import settings
from app.content.datasets_routes import create_router
from app.content.routes import read_news
from app.shared.readonly_database import open_readonly_factory


SQL = """
SELECT id, source, external_id, content_type, published_at, title, source_url, securities, copyright_status,
       CASE WHEN copyright_status IN ('licensed','public_domain') THEN summary END AS summary,
       CASE WHEN copyright_status IN ('licensed','public_domain') THEN body END AS body
FROM core.content_document
WHERE published_at <= :now AND copyright_status IN ('licensed','public_domain','link_only')
ORDER BY published_at DESC, id DESC
"""


def main():
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        now = session.scalar(text("SELECT CURRENT_TIMESTAMP"))
        params = {"now":now}
        explain = session.scalar(text("EXPLAIN (FORMAT JSON) " + SQL + " LIMIT 101"), params)
        expected = iter(session.execute(text(SQL), params).mappings().all())
        tally, rights = Counter(), Counter()
        app = FastAPI()
        app.include_router(create_router(factory))
        public_rows = []  # 新闻产品首屏所需的五条独立样本
        queries = []
        event.listen(factory.kw["bind"], "before_cursor_execute", lambda *args: queries.append(1))
        pages, cursor = 0, None
        elapsed = perf_counter()
        with TestClient(app) as client:
            while True:
                filters = {"published_through":now.isoformat(), "limit":100}
                if cursor:
                    filters["cursor"] = cursor
                start = len(queries)
                response = client.get("/api/v1/datasets/content-documents", params=filters)
                response.raise_for_status()
                assert len(queries) == start + 1
                result = response.json()
                for row in result["data"]:
                    row["published_at"] = datetime.fromisoformat(row["published_at"])
                    assert row == dict(next(expected)), row["id"]
                    tally[row["content_type"]] += 1
                    rights[row["copyright_status"]] += 1
                    if row["content_type"] == "news" and len(public_rows) < 5:
                        public_rows.append(row)
                pages += 1
                cursor = result["meta"]["next_cursor"]
                if cursor is None:
                    break
        assert next(expected, None) is None
        ms = round((perf_counter() - elapsed) * 1000)
        product = read_news(factory, limit=5, now=now)["items"]
        assert [str(r["id"]) for r in public_rows] == [r["id"] for r in product]
        for fact, item in zip(public_rows, product):
            assert item["title"] == fact["title"] and item["sourceUrl"] == fact["source_url"]
            assert datetime.fromisoformat(item["publishedAt"]) == fact["published_at"]
            assert item.get("body") == fact["body"] and item.get("summary") == fact["summary"]
        print(json.dumps(dict(checkedAt=now.astimezone(timezone.utc).isoformat(),
            rows=sum(tally.values()), byType=tally, byRights=rights, pages=pages, ms=ms,
            sqlPerPage=1, plan=explain[0]["Plan"]["Node Type"], matchedNews=len(product)), ensure_ascii=True))


if __name__ == "__main__":
    main()
