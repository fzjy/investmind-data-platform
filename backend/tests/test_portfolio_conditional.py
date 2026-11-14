from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.db.models import PublishedSnapshot
from app.portfolio.database_provider import PortfolioDatabaseProvider
from app.portfolio.routes import create_router
from app.shared.repository import SnapshotRepository
from backend.tests.jobs.test_build_and_publish_integration import _factory


def test_conditional_get_reads_metadata_only_and_invalidates_on_freshness_and_release():
    factory = _factory()
    with factory.begin() as session:
        session.add(PublishedSnapshot(product="portfolio", version="v1", as_of=datetime.now(timezone.utc),
            payload={"assets": [], "prices": {}, "snapshotLabel": "v1"}, stale=False))
    api = FastAPI()
    api.include_router(create_router(provider=PortfolioDatabaseProvider(SnapshotRepository(factory))))
    statements = []
    event.listen(factory.kw["bind"], "before_cursor_execute", lambda _, __, sql, *args: statements.append(sql))
    with TestClient(api) as client:
        url = "/api/v1/portfolio"
        first = client.get(url)
        assert first.status_code == 200 and len(statements) == 1
        tag = first.headers["etag"]
        statements.clear()
        same = client.get(url, headers={"If-None-Match": tag})
        assert same.status_code == 304 and same.content == b"" and same.headers["etag"] == tag
        assert len(statements) == 1 and ".payload" not in statements[0]
        with factory.begin() as session:
            session.get(PublishedSnapshot, "portfolio").stale = True
        changed = client.get(url, headers={"If-None-Match": tag})
        assert changed.status_code == 200 and changed.json()["meta"]["stale"]
        assert changed.headers["etag"] != tag
        with factory.begin() as session:
            current = session.get(PublishedSnapshot, "portfolio")
            current.version = "v2"
            current.payload = {**current.payload, "snapshotLabel": "v2"}
        changed = client.get(url, headers={"If-None-Match": changed.headers["etag"]})
        assert changed.status_code == 200 and changed.json()["data"]["snapshotLabel"] == "v2"
        assert client.get("/api/data/portfolio", headers={"If-None-Match": changed.headers["etag"]}).status_code == 200
