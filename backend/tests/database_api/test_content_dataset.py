from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.db.models import ContentDocument
import app.main as main


PATH = "/api/v1/datasets/content-documents"
DAY = datetime(2026, 3, 3, 1, tzinfo=timezone.utc)


def test_documents_and_product_share_visibility_and_preserve_pagination(pg_factory, pg_schema_url, monkeypatch):
    with pg_factory.begin() as session:
        for name, status, kind, day in (
            ("one", "public_domain", "news", DAY), ("two", "link_only", "news", DAY),
            ("notice", "licensed", "announcement", DAY), ("hidden", "restricted", "news", DAY),
            ("later", "licensed", "news", datetime.now(timezone.utc) + timedelta(days=3)),
        ):
            session.add(ContentDocument(title=name, dedupe_key=name, source="sina-finance",
                external_id=name, source_url="https://example.com/", summary="excerpt", body="text",
                copyright_status=status, published_at=day, content_type=kind, securities=["600519.SH"]))
    monkeypatch.setattr(main, "settings", Settings(database_read_url=pg_schema_url))
    application = main.create_app()
    try:
        with TestClient(application) as client:
            params = dict(content_type="news", symbol="600519.SH", limit=1, published_from=DAY.isoformat())
            first = client.get(PATH, params=params)
            assert first.status_code == 200, first.text
            row, = first.json()["data"]
            assert row["title"] == "two" and row["body"] is None and row["summary"] is None
            assert row["source"] == "sina-finance"
            cursor = first.json()["meta"]["next_cursor"]
            second = client.get(PATH, params={**params, "cursor":cursor}).json()
            assert second["meta"]["next_cursor"] is None
            assert second["data"][0]["body"] == "text"
            assert second["data"][0]["title"] == "one"
            same_in_shanghai = {**params, "published_from":"2026-03-03T09:00:00+08:00", "cursor":cursor}
            assert client.get(PATH, params=same_in_shanghai).json() == second
            product = client.get("/api/v1/news").json()["items"]
            assert [r["title"] for r in product] == ["two", "one"]
            assert product[0]["source"] == "新浪财经" and "body" not in product[0]
            assert client.get(PATH, params={**params, "content_type":"announcement", "cursor":cursor}).status_code == 400
            for invalid_time in ("2026-03-03T00:00:00", "1772500000", "0001-01-01T00:00:00+14:00"):
                assert client.get(PATH, params={"published_from":invalid_time}).status_code == 400
            assert client.get(PATH + "?symbol=600519.SH&symbol=000001.SZ").status_code == 400
            assert client.get(PATH, params={"symbol":"000001.SZ"}).json()["data"] == []
            assert client.get(PATH, params={"content_type":"announcement", "published_through":DAY.isoformat()}).json()["data"][0]["title"] == "notice"
    finally:
        application.state.read_engine.dispose()
