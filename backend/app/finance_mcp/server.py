"""仅允许回环客户端使用独立本机数据令牌的 Streamable HTTP 服务。"""
from __future__ import annotations

import hmac
import ipaddress
import os
import re
import secrets
import tempfile
from pathlib import Path

from mcp.server import MCPServer
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.finance_mcp.tools import register_tools


def finance_token() -> bytes:
    configured = os.environ.get("INVESTMIND_FINANCE_TOKEN_FILE")
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    path = Path(configured) if configured else base / "InvestMind" / "finance-mcp.token"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        # 先写完再独占发布；并发启动共用首个成功发布的令牌。
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
            name = temp.name
            temp.write(secrets.token_urlsafe(32).encode("ascii"))
        try:
            try:
                os.link(name, path)
            except FileExistsError:
                pass
        finally:
            Path(name).unlink()
    with path.open("rb") as token_file:
        token = token_file.read(514).strip()
    if not re.fullmatch(rb"[A-Za-z0-9._~-]{32,512}", token):
        raise ValueError("金融数据令牌文件格式不合法")
    return b"Bearer " + token


def local_peer(scope, headers):
    peer = scope.get("client")
    try:
        local = peer is not None and ipaddress.ip_address(peer[0]).is_loopback
    except ValueError:
        return False
    host = headers.get("host", "").lower()
    return (local and len(headers.getlist("host")) == 1
            and re.fullmatch(r"(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?", host)
            and headers.getlist("origin") in ([], ["http://" + host]))


class FinanceAccess:
    def __init__(self, app, *, only, identity):
        self.app = app
        self.only = only
        self.identity = identity

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or (not self.only and path.rstrip("/") != "/mcp"):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        status, code, message = 403, "ACCESS_DENIED", "当前请求未获本机金融读取权限"
        if local_peer(scope, headers):
            method = scope["method"]
            if self.only and (method, path) == ("GET", "/health"):
                return await self.app(scope, receive, send)
            authorization = headers.getlist("authorization")
            if len(authorization) != 1 or not hmac.compare_digest(authorization[0].encode("latin-1"), self.identity):
                status, code, message = 401, "AUTHENTICATION_REQUIRED", "需要有效的金融数据令牌"
            elif (path == "/mcp" or
                  (method == "POST" and path == "/api/v1/stocks/screener") or
                  (method == "GET" and (path in ("/api/v1/search", "/api/v1/data-status") or
                   re.fullmatch(r"/api/v1/stocks/[0-9]{6}\.(SH|SZ|BJ)", path)))):
                return await self.app(scope, receive, send)
        response = JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                                headers={"Cache-Control": "no-store"})
        return await response(scope, receive, send)


def attach(application, *, only, **services):
    server = MCPServer("investmind-finance", version="1.0", subscriptions=False)
    register_tools(server, **services)
    transport = server.streamable_http_app(stateless_http=True, json_response=True, max_request_body_size=64 * 1024)
    # 精确匹配 /mcp，避免子应用挂载自动重定向到 /mcp/，丢失身份或 POST。
    application.router.routes.append(Route("/mcp", endpoint=transport))
    application.add_middleware(FinanceAccess, only=only, identity=finance_token())
    return transport
