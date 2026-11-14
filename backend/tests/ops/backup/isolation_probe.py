"""故意吞掉拒绝异常；Node 验证仍必须出现97，不启动任何真实工具。"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from app.ops.backup.config import parse_target
from app.ops.backup.process import run_process
from app.ops.backup.tools import NativeTools
from app.ops.backup.api_probe import probe_restored_api
from app.ops.backup.contracts import DatabaseInventory


if __name__ == "__main__":
    reached = False
    def controlled_terminal(*args, **kwargs):
        global reached
        reached = True
        raise AssertionError("controlled terminal reached")
    subprocess.Popen = controlled_terminal
    try:
        if sys.argv[1] == "launch":
            run_process(["pg_dump", "--version"], env={}, timeout=1)
        elif sys.argv[1] == "api":
            probe_restored_api(parse_target("postgresql://canary:canary@127.0.0.1:1/canary"), DatabaseInventory(tables=()), timeout=1)
        else:
            adapter = NativeTools.__new__(NativeTools)
            adapter.executables = {"pg_dump": "pg_dump", "pg_restore": "pg_restore"}
            with tempfile.TemporaryDirectory(prefix="smartparse-isolation-probe-") as temporary:
                adapter.dump(parse_target("postgresql://canary:canary@127.0.0.1:1/canary"), "1-2-3", Path(temporary) / "database.dump", 1)
    except AssertionError:
        pass
    print(json.dumps({"adapterReached": reached}))
