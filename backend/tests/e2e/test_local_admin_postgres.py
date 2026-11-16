import os
from pathlib import Path
from shutil import copytree
from types import SimpleNamespace
from io import BytesIO

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from testcontainers.community.postgres import PostgresContainer
import pytest
from app.main import create_app
from app.local_admin.routes import mount_admin
from app.local_admin.workers import LocalManager
from app.db.models import JobRun, JobCursor, SchedulerHeartbeat, JobFailure, QualityCheckResult

pytestmark = pytest.mark.skipif(os.environ.get("DANG_RUN_POSTGRES_TESTS")!="1", reason="disposable PostgreSQL")


def test_local_entry_apply_and_run_links(tmp_path):
    root = Path(__file__).resolve().parents[3]
    copytree(root / "config", tmp_path / "config")
    spawned = []
    def launch(name):
        spawned.append(name)
        return SimpleNamespace(stdin=BytesIO(), poll=lambda:None, wait=lambda:None)
    manager = LocalManager(tmp_path, launch)
    manager.start()
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as pg:
        engine = create_engine(pg.get_connection_url())
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE SCHEMA ops")
            for model in (JobRun,JobCursor,SchedulerHeartbeat,JobFailure,QualityCheckResult):
                model.__table__.create(conn)
            conn.execute(JobRun.__table__.insert(),dict(id="example",job_name="sync-daily",business_key="today"))
        app = create_app()
        app.state.read_engine = engine
        mount_admin(app, project_root=root, manager=manager)
        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            headers = {"origin":"http://127.0.0.1:8000","x-admin-token":client.get("/admin/api/session").json()["token"]}
            config = client.get("/admin/api/config/tasks").json()
            patch = {"version":config["version"],"changes":{"stocks":True}}
            saved = client.post("/admin/api/config/tasks/apply",headers=headers,json=patch)
            assert saved.status_code == 200 and spawned == ["scheduler"]
            assert client.post("/admin/api/config/tasks/apply",headers=headers,json=patch).status_code == 409
            patch["version"]=saved.json()["version"]
            assert client.post("/admin/api/config/tasks/apply",headers=headers,json=patch).status_code == 200
            assert len(spawned)==1
            tasks = client.get("/admin/api/tasks").json()
            assert next(t for t in tasks if t["name"]=="stocks")["applyState"]=="已保存，待应用"
            run = client.get("/admin/api/tasks/stocks/runs/example").json()
            assert "待确认" in run["runs"][0]["displayStatus"]
            assert client.get("/admin/api/tasks/macro/runs/example").status_code==404
            assert client.get("/admin/api/tables/ops/job_failure?run=example").json()["rows"] == []
        engine.dispose()
