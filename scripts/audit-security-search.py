"""用当前发布版本、只读数据库和真实 HTTP 核验证券搜索；可附测网站生产构建。"""
from argparse import ArgumentParser
from contextvars import ContextVar
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import event, text
from app.config import settings
from app.db.models import PublishedSnapshot
from app.search.routes import create_router
from app.shared.errors import DataServiceError, error_payload
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider
from app.portfolio.database_provider import PortfolioDatabaseProvider
from app.portfolio.routes import create_router as portfolio_router


def get(url):
    try:
        with urlopen(url, timeout=25) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def rank(row, query):
    symbol = row["symbol"].lower()
    name = unicodedata.normalize("NFKC", row["name"]).strip().casefold()
    fields = [symbol, symbol[:6], name]
    if query in fields[:2]:
        return 0
    if query == name:
        return 1
    if any(v.startswith(query) for v in fields):
        return 2
    return 3 if any(query in v for v in fields) else 4


def audit(output, website):
    calls = []
    context = ContextVar("search_sql", default=None)
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        snap = session.get(PublishedSnapshot, "screener")
        rows = snap.payload["stocks"]
        assert rows and len({r["symbol"] for r in rows}) == len(rows)
        @event.listens_for(factory.kw["bind"], "before_cursor_execute")
        def capture(_conn, _cursor, stmt, *_args):
            if context.get() is not None:
                context.get().append("mart.published_snapshot" if "mart.published_snapshot" in stmt else "other")
        app = FastAPI()
        app.include_router(create_router(StocksDatabaseProvider(SnapshotRepository(factory))))
        if website:
            app.include_router(portfolio_router(provider=PortfolioDatabaseProvider(SnapshotRepository(factory))))
        @app.exception_handler(DataServiceError)
        async def handled(_request, exc):
            return JSONResponse(error_payload(exc,str(uuid4())),status_code=exc.status)
        @app.middleware("http")
        async def count(request, next_call):
            reads = []
            token = context.set(reads)
            try:
                response = await next_call(request)
                calls.append(dict(path=request.url.path, sql=reads))
                return response
            finally:
                context.reset(token)
        sock = socket.socket()
        sock.bind(("127.0.0.1",0))
        api = "http://127.0.0.1:" + str(sock.getsockname()[1])
        server = uvicorn.Server(uvicorn.Config(app,log_level="error"))
        runner=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True)
        runner.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        web=None
        try:
            record = {}
            for key,params in [("idle",{}),("fullwidth",{"q":"６００５１９.sh"}),("exact-name",{"q":rows[0]["name"]}),
                               ("substring",{"q":"银行"}),("prefix-first",{"q":"6","pageSize":2}),
                               ("prefix-next",{"q":"6","page":2,"pageSize":2}),("past-last",{"q":"6","page":999}),
                               ("no-results",{"q":"不存在的证券关键词"}),("literal-wildcard",{"q":"%"})]:
                query=unicodedata.normalize("NFKC",params.get("q","")).strip()
                page=params.get("page",1)
                matches=sorted((rank(r,query.casefold()),r["symbol"]) for r in rows) if query else []
                matches=[s for score,s in matches if score<4]
                reports=[]
                for path in ("/api/data/search","/api/v1/search"):
                    status,body=get(api+path+"?"+urlencode(params))
                    assert status==200
                    payload=json.loads(body)
                    data=payload["data"]
                    stocks=data["stocks"]
                    assert data["query"]==query and set(data)=={"query","stocks"} and set(payload["meta"])=={"requestId"}
                    size=stocks["pageSize"]
                    codes=[r["symbol"] for r in stocks["items"]]
                    assert codes == matches[(page-1)*size:page*size] and stocks["page"]==page
                    assert all(set(r)=={"symbol","code","name"} for r in stocks["items"])
                    assert stocks["status"]==("ready" if query else "idle") and stocks["total"]==(len(matches) if query else None)
                    assert calls[-1]["sql"]==(["mart.published_snapshot"] if query else [])
                    if query:
                        assert stocks["meta"]["version"]==snap.version
                    reports.append(data)
                assert reports[0]==reports[1]
                record[key]={"q":query,"page":page,"total":stocks["total"],"symbols":codes,"sqlPerRequest":len(calls[-1]["sql"])}
            for suffix in ("pageSize=201","q=x&q=y","page=0","q="+"a"*129):
                status,_=get(api+"/api/v1/search?"+suffix)
                assert status==400 and not calls[-1]["sql"]
            result={"version":snap.version,"asOf":snap.as_of.isoformat(),"universe":len(rows),"api":record,"invalidRequestsWithoutSql":4}
            if website:
                with socket.socket() as free:
                    free.bind(("127.0.0.1",0))
                    port=free.getsockname()[1]
                site=f"http://127.0.0.1:{port}"
                env={**os.environ,"BACKEND_INTERNAL_API_BASE_URL":api,"NEXT_TELEMETRY_DISABLED":"1"}
                web=subprocess.Popen(["node","node_modules/next/dist/bin/next","start","-H","127.0.0.1","-p",str(port)],
                                     cwd=website,env=env,stdout=subprocess.DEVNULL)
                for _ in range(150):
                    try:
                        with socket.create_connection(("127.0.0.1",port),.25):
                            break
                    except OSError:
                        time.sleep(.1)
                site_evidence={}
                for path in ("/api/data/search","/backend-api/api/data/search","/search"):
                    for q in ("600519","宏观",""):
                        before=len(calls)
                        status,content=get(site+path+"?"+urlencode({"q":q}))
                        assert status==200,(path,status)
                        actual=[c for c in calls[before:] if c["path"]=="/api/v1/search"]
                        assert len(actual)==1 and len(actual[0]["sql"])==bool(q),(path,q,actual)
                        if path=="/search":
                            html=content.decode()
                            expected="/stocks/600519.SH" if q=="600519" else "查看宏观指标与分析" if q else "浏览已实现功能入口"
                            assert expected in html,(path,q)
                        else:
                            body=json.loads(content)
                            assert body["meta"]["pageCatalogVersion"]=="site-pages-v1"
                            if q=="600519":
                                assert body["data"]["stocks"]["items"][0]["href"]=="/stocks/600519.SH"
                            else:
                                assert {r["id"] for r in body["data"]["pages"]["items"]}==({"macro"} if q else {"home","market","screener","macro"})
                        extra=[c for c in calls[before:] if c["path"]!="/api/v1/search"]
                        assert all(c["path"]=="/api/v1/portfolio" and len(c["sql"])==1 for c in extra)
                        site_evidence[path+"?q="+q]={"searchHttp":1,"searchSql":len(actual[0]["sql"]),
                                                      "layoutCalls":extra,"status":status}
                result["site"]=site_evidence
                result["buildId"]=(website/".next"/"BUILD_ID").read_text().strip()
            output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
            print(f"{len(record)} 组新旧接口及 4 个无效请求通过；网站访问 {len(result.get('site',{}))} 次。")
        finally:
            if web:
                web.terminate()
                web.wait(timeout=15)
            server.should_exit=True
            runner.join(timeout=10)


if __name__=="__main__":
    parser=ArgumentParser(description=__doc__)
    parser.add_argument("--website",type=Path,help="可选，已构建的网站仓目录；同时验证兼容路由与 SSR")
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    audit(args.output,args.website.resolve() if args.website else None)
