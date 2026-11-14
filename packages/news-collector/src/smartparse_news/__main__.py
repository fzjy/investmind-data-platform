import argparse
import json
from pathlib import Path
from .collector import Collector, Config


def main():
    parser = argparse.ArgumentParser(description='独立新闻采集：状态、单次有界采集、待交付元数据')
    parser.add_argument('command', choices=['run', 'status', 'pending'])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    args = parser.parse_args()
    try:
        collector = Collector(args.state, Config.read(args.config))
        value = getattr(collector, args.command)()
        print(json.dumps(value, ensure_ascii=False))
        return 1 if isinstance(value, dict) and value.get('status') in ('failed', 'locked') else 0
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'errorType': type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
