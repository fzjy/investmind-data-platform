"""宏观历史桥接沿用现有财务配置，不新增环境变量或预算。"""
import json
from pathlib import Path
from app.config import PROJECT_ROOT

CONFIG_FILE = PROJECT_ROOT / 'config' / 'financial-indicators.json'


def load_stock_history_policy(path: Path | None = None) -> bool:
    value = json.loads((path or CONFIG_FILE).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('MACRO_HISTORY_POLICY_INVALID')
    if 'macroHistoryBridge' not in value:
        return False
    policy = value['macroHistoryBridge']
    if (not isinstance(policy, dict) or set(policy) != {'enabled', 'contractVersion'}
            or type(policy['enabled']) is not bool
            or policy['contractVersion'] != 'stock-raw-macro-history-v1'):
        raise ValueError('MACRO_HISTORY_POLICY_INVALID')
    return policy['enabled']
