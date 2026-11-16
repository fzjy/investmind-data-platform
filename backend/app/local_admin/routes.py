from __future__ import annotations

import hmac
import html
import re
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from app.local_admin import status, tables
from app.local_admin.configuration import Configuration, Conflict
from fastapi.staticfiles import StaticFiles

STATIC = Path(__file__).with_name("static")


class Patch(BaseModel):
    version: str
    changes: dict


def mount_admin(app: FastAPI, *, project_root: Path, manager=None):
    admin = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    token = secrets.token_urlsafe(32)

    @admin.middleware("http")
    async def local_access(request: Request, call_next):
        host = request.headers.get("host", "")
        origin = request.headers.get("origin")
        allowed = re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::\d{1,5})?", host)
        same = origin == "http://" + host
        if not allowed or (origin and not same) or request.headers.get("sec-fetch-site") == "cross-site":
            return PlainTextResponse("仅允许本机同源访问", 403)
        if request.method not in {"GET", "HEAD"}:
            supplied = request.headers.get("x-admin-token", "").encode()
            if not same or not hmac.compare_digest(supplied, token.encode()):
                return PlainTextResponse("页面凭据已失效，请刷新", 403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"})
        return response

    @admin.get("/")
    def page():
        return FileResponse(STATIC / "index.html")

    @admin.get("/api/session")
    def session():
        return {"token": token}

    @admin.get("/docs/{name:path}")
    def document(name: str):
        docs = (project_root / "docs").resolve()
        path = (docs / name).resolve()
        if path.suffix not in {".md", ".json"} or not path.is_relative_to(docs) or not path.is_file():
            raise HTTPException(404)
        if path.suffix == ".json":
            return FileResponse(path, media_type="application/json")
        def link(match):
            label, dest = match.groups()
            target = (path.parent / dest.split("#")[0]).resolve()
            if target.is_relative_to(docs) and target.suffix in {".md", ".json"} and target.is_file():
                return f'<a href="/admin/docs/{html.escape(target.relative_to(docs).as_posix(), quote=True)}">{label}</a>'
            return label
        body = re.sub(r"\[([^\]\n]+)\]\(([^)\n]+)\)", link, html.escape(path.read_text(encoding="utf-8")))
        return HTMLResponse('<meta charset=utf-8><link rel=stylesheet href="/admin/static/admin.css">'
                            '<article class="document"><a href="/admin/#docs">返回接口文档</a><pre>' + body + '</pre></article>')

    engine = app.state.read_engine
    config = manager.config if manager else Configuration(project_root)

    @admin.exception_handler(KeyError)
    async def missing(_request, _error):
        return JSONResponse({"detail": "记录或目录不存在，请重新载入"}, 404)

    @admin.exception_handler(SQLAlchemyError)
    async def database_error(_request, _error):
        return JSONResponse({"detail": "数据库查询失败或超时，请检查本机数据库连接"}, 503)

    @admin.get("/api/tables")
    def catalog():
        return tables.list_tables(engine)

    @admin.get("/api/tables/{schema}/{name}")
    def records(schema: str, name: str, cursor: int = Query(0, ge=0), run: str | None = None, batch: str | None = None):
        if run and (schema, name) not in tables.RUN_RELATIONS:
            raise HTTPException(400, "此表不支持按运行编号查询")
        if batch and (schema != "raw" or name not in tables.BATCH_TABLES):
            raise HTTPException(400, "此表不支持按批次查询")
        return tables.preview(engine, schema, name, cursor, run, batch)

    @admin.get("/api/tables/{schema}/{name}/cell")
    def value(schema: str, name: str, column: str, row: str, start: int = Query(0, ge=0)):
        return tables.cell(engine, schema, name, column, row, start)

    @admin.get("/api/config/{name}")
    def configuration(name: str):
        return config.read(name)

    @admin.post("/api/config/{name}/apply")
    def apply(name: str, patch: Patch):
        if manager is None:
            raise HTTPException(503, "本机启动器未连接")
        try:
            return manager.apply(name, patch.version, patch.changes)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, TypeError):
            raise HTTPException(422, "配置值无效，请检查字段与取值范围") from None

    @admin.get("/api/tasks")
    def tasks():
        if manager is None:
            raise HTTPException(503, "本机启动器未连接")
        return status.task_rows(manager)

    @admin.get("/api/tasks/{name}/runs")
    def runs(name: str, offset: int = Query(0, ge=0)):
        result = status.run_history(engine, name, offset=offset)
        recent = manager.store.recent(name, offset) if manager else []
        result.update(localRuns=recent[:20], more=result["more"] or len(recent) > 20)
        return result

    @admin.get("/api/tasks/{name}/runs/{run_id}")
    def run(name: str, run_id: str):
        return status.run_history(engine, name, run_id)

    admin.mount("/static", StaticFiles(directory=STATIC))
    app.mount("/admin", admin)
    return admin
