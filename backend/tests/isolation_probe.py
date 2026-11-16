"""仅由Node入口回归调用；所有故意被拒绝的访问必须先经过离线guard。"""
import json
import sys

import httpx
import requests
from sqlalchemy import create_engine, text

import app.config as config


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "postgres":
        from backend.tests.support.postgres import child_url
        engine = create_engine(child_url)
        with engine.connect() as connection:
            value = connection.scalar(text('select 1'))
            shared = connection.scalar(text('select id from child_probe'))
        engine.dispose()
        print(json.dumps({'postgres': value, 'shared': shared,
                          'pgEnvironment': [key for key in __import__('os').environ if key.upper().startswith('PG')],
                          'dotenvDisabled': config.PROJECT_ENV_FILE.name == 'missing.env'}))
    elif mode == "allowed":
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            status = client.get("https://example.invalid").status_code
        print(json.dumps({"backend": config.Settings.from_env().data_backend,
                          "dotenvDisabled": config.PROJECT_ENV_FILE.name == "missing.env" and not config.PROJECT_ENV_FILE.exists(),
                          "mockHttp": status}))
    elif mode in {"requests-send", "docker-send"}:
        adapter_reached = False

        class MemoryAdapter(requests.adapters.BaseAdapter):
            def send(self, request: requests.PreparedRequest, **kwargs: object) -> requests.Response:
                global adapter_reached
                adapter_reached = True
                response = requests.Response()
                response.status_code = 200
                response._content = b"controlled response"
                response.request = request
                return response

            def close(self) -> None:
                pass

        if mode == "docker-send":
            from docker.api.client import APIClient

            # Real SDK class, but skip construction that reads Docker settings/connects.
            session = APIClient.__new__(APIClient)
            requests.Session.__init__(session)
        else:
            session = requests.Session()
        with session:
            # A terminal memory adapter prevents real I/O even against the broken guard.
            session.mount("https://", MemoryAdapter())
            prepared = session.prepare_request(requests.Request("GET", "https://example.invalid"))
            try:
                session.send(prepared)
            except AssertionError:
                pass
        print(json.dumps({"adapterReached": adapter_reached}))
    else:
        try:
            if mode == "http":
                httpx.get("https://example.invalid")
            elif mode == "requests":
                requests.get("https://example.invalid")
            elif mode == "database":
                create_engine("postgresql+psycopg://canary:canary@127.0.0.1:1/canary").connect()
        except AssertionError:
            pass  # The runner must still exit97 when a target swallows a violation.
