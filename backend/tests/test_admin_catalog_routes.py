from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
from app.local_admin.routes import mount_admin
from app.local_admin.catalog_runtime import runtime_context

ROOT=Path(__file__).resolve().parents[2]


def test_catalog_readable_when_database_offline():
    app=create_app()
    original=app.openapi()["paths"]
    mount_admin(app,project_root=ROOT)
    with TestClient(app,base_url="http://127.0.0.1:11383") as client:
        response=client.get("/admin/api/catalog")
        assert response.status_code==200
        assert len(response.json()["entries"])>40
        detail=client.get("/admin/api/catalog/stock-dividend-yield").json()
        assert detail["entry"]["history"]["records"]["value"] is True
        assert detail["operations"][0]["available"]
        assert client.get("/admin/api/catalog/unknown").status_code==404
        assert client.get("/admin/api/catalog",headers={"host":"remote.example"}).status_code==403
        assert app.openapi()["paths"]==original
    def offline():
        raise RuntimeError("offline")
    ctx=runtime_context(None,offline)
    assert ctx["tableState"]=="unknown" and ctx["issues"]
