from datetime import UTC, datetime
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.db.models import PageSnapshot
from app.macro.database_provider import MacroDatabaseProvider
from app.macro.routes import create_router
from app.macro.service import MacroService
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.marts.publication import SnapshotPublisher
from app.shared.contracts import DataMeta, ProviderSnapshot
from app.shared.errors import DataServiceError
from app.shared.repository import SnapshotRepository
from backend.tests.jobs.test_build_and_publish_integration import _factory


def test_existing_two_routes_can_read_same_historical_release_without_current_pointer() -> None:
    factory = _factory()
    cutoff = datetime(2026, 9, 20, tzinfo=UTC)
    candidates = MacroMartBuilder(MacroRepository(factory)).build(cutoff, "old")
    publisher = SnapshotPublisher(factory)
    publisher.publish_many(candidates, "release-old", ())
    publisher.publish_many(MacroMartBuilder(MacroRepository(factory)).build(datetime(2026, 9, 21, tzinfo=UTC), "new"), "release-new", ())
    app = FastAPI()
    app.include_router(create_router(provider=MacroDatabaseProvider(SnapshotRepository(factory))))
    client = TestClient(app)
    for product in ("macro-transmission", "macro-insights"):
        response = client.get(f"/api/data/{product}?version=release-old")
        assert response.status_code == 200
        assert response.json()["meta"]["version"] == "release-old"
        assert response.json()["meta"]["source"] == "database"


@pytest.mark.parametrize(("source", "version"), [("fixture", "wanted"), ("database", "other")])
def test_historical_read_cannot_bypass_backend_or_product_release_check(source: str, version: str) -> None:
    class WrongRepository:
        def read_versioned(self, product: str, requested: str) -> ProviderSnapshot[dict[str, object]]:
            payload = {key: [] for key in ("compassPoints", "sandboxGroups", "sandboxIndicators", "sandboxMonths", "sandboxPresets", "statusCards", "trendDefinitions")}
            return ProviderSnapshot(payload, DataMeta(source=source, asOf="2026-09-20T00:00:00Z", version=version, stale=False))
    service = MacroService(MacroDatabaseProvider(WrongRepository()))
    with pytest.raises(DataServiceError) as error:
        service.get_insights("wanted")
    assert error.value.code == "INVALID_SOURCE_PAYLOAD"
