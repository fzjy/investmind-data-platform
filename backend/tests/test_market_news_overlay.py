from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.content.routes import read_news
from app.db.models import JobCursor
from app.market.routes import create_router

from backend.tests.support.fixtures import FixtureStore
from backend.tests.support.providers import MarketFixtureProvider


@pytest.mark.parametrize("latest", ["bad-stamp", "2026-11-01T08:00:00"])
def test_invalid_news_health_keeps_market_snapshot_available(pg_factory, latest):
    factory = pg_factory
    with factory.begin() as session:
        session.add(JobCursor(job_name="news-collector", cursor_key="sina-finance",
                              cursor_value={"latestPublishedAt": latest}))
    app = FastAPI()
    app.include_router(create_router(provider=MarketFixtureProvider(FixtureStore.default()),
                                     latest_news=lambda: read_news(factory)))
    client = TestClient(app, raise_server_exceptions=False)
    snapshot = client.get("/api/v1/market/explorer")
    combined = client.get("/api/v1/market/explorer?news=latest")
    assert snapshot.status_code == combined.status_code == 200
    assert combined.json() == snapshot.json()
    assert "liveNewsPage" not in combined.json()["data"]
