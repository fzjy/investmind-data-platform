from pathlib import Path
from fastapi.testclient import TestClient
from app.main import create_app
from app.local_admin.routes import mount_admin

ROOT = Path(__file__).resolve().parents[2]


def test_admin_only_on_local_launcher():
    app = create_app()
    public = app.openapi()["paths"]
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    assert client.get("/admin/").status_code == 404
    mount_admin(app, project_root=ROOT)
    assert client.get("/admin/").status_code == 200
    assert client.get("/admin/docs/platform-api.md").status_code == 200
    assert client.get("/admin/docs/.env").status_code == 404
    assert client.get("/admin/api/session", headers={"host": "remote.example"}).status_code == 403
    assert client.post("/admin/api/config/tasks/apply", headers={"origin": "https://bad.example"}).status_code == 403
    assert client.post("/admin/api/config/tasks/apply", headers={"origin": "http://127.0.0.1:8000"}).status_code == 403
    assert app.openapi()["paths"] == public
