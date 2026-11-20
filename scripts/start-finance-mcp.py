"""启动桌面端使用的本机只读金融 HTTP 与 MCP 服务。"""
import os
import sys
from pathlib import Path

import uvicorn


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / "backend"), str(root / "packages/news-collector/src")]
    from app.main import create_app

    app = create_app(finance_only=True)
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("INVESTMIND_FINANCE_PORT", "11384")),
                proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
