from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.portfolio.routes import create_router


def test_public_portfolio_api_excludes_private_analysis():
    class PublicInputs:
        def read_portfolio(self):
            raise AssertionError("private payload must never reach the public provider")

    api = FastAPI()
    api.include_router(create_router(provider=PublicInputs()))
    with TestClient(api) as client:
        assert client.post("/api/data/portfolio/analysis", json={"holdings": []}).status_code == 404
    assert "/api/data/portfolio" in api.openapi()["paths"]
