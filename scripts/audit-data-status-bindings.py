"""离线列出目录绑定及校验问题。"""
import json
import sys
from dataclasses import asdict
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root/'backend'))

from app.acquisition.catalog import load_bindings, validate_bindings

if __name__ == '__main__':
    issues = validate_bindings(root)
    print(json.dumps({'bindings':[asdict(item) for item in load_bindings(root)], 'issues':issues},
                     ensure_ascii=True))
    raise SystemExit(bool(issues))
