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
from app.local_admin.catalog import load_catalog, validate_catalog
from app.local_admin.catalog_contracts import describe_operation
from app.local_admin.catalog_runtime import runtime_context
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
    from app.acquisition.routes import create_router as create_status_router
    from app.acquisition.service import StatusService
    from sqlalchemy.orm import sessionmaker
    status_service = getattr(app.state, 'acquisition_status', None) or StatusService(sessionmaker(engine),root=project_root)
    admin.include_router(create_status_router(status_service,path='/api/data-status'))
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

    @admin.get("/api/catalog")
    def data_catalog():
        catalog = load_catalog(project_root)
        return {"version": catalog.version, "categories": catalog.categories,
                "entries": [{"id":e.id, "name":e.name, "category":e.category, "status":e.status,
                    "fields":[{"id":f.id,"name":f.name} for f in e.fields],
                    "search":" ".join([e.id,e.name,*[s.get("name","") for s in e.sources],
                        *[s.get("api","") for s in e.sources],*[t.schema_+"."+t.table for t in e.tables],
                        *[f.id+" "+f.name for f in e.fields]])} for e in catalog.entries]}

    @admin.get("/api/catalog-runtime")
    def data_catalog_runtime():
        return runtime_context(manager, lambda: tables.list_tables(engine))

    @admin.get("/api/catalog/{entry_id}")
    def data_catalog_entry(entry_id: str):
        catalog = load_catalog(project_root)
        entry = next((e for e in catalog.entries if e.id == entry_id),None)
        if entry is None:
            raise KeyError(entry_id)
        spec = app.openapi()
        operations = [{**access, **describe_operation(spec,access["method"],access["path"])}
                      for access in entry.access if access.get("path")]
        return {"entry":entry.model_dump(by_alias=True), "operations":operations,
                "issues":[i for i in validate_catalog(catalog,project_root=project_root,openapi=spec)
                          if i["entryId"] in {"",entry_id}]}

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
