"""从当前文件/定义只读收集目录覆盖身份。"""
import json
import importlib.util
from collections import defaultdict
from pathlib import Path


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def source_contracts(root: Path):
    spec = importlib.util.spec_from_file_location("catalog_contract_inventory",root/"scripts/audit-tushare-field-consumers.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    collected=defaultdict(module.field_record)
    module.configured(root,collected)
    return set(collected)


def identities(root: Path) -> set[str]:
    from app.connectors.tushare.strategies import FIELD_REGISTRY, STANDARD_API_NAMES, VIP_API_NAMES
    from app.stocks.derived.registry import FORMULAS
    from app.stocks.derived.workbench_cards import DEFINITIONS, CARD_VERSION
    from app.stocks.derived.workbench_risks import RULES, VERSION
    from app.macro.derivations import SPREADS
    from app.connectors.external_files.contracts import MacroImportRecord, ContentImportRecord
    from app.stocks.rea_fields import REA_THRESHOLD_ROWS
    required = set()
    required.update(f"tushare:{api}.{field}" for api,field in source_contracts(root))
    report = read_json(root / "docs/research/investmind-tushare-field-consumers.json")
    for row in report["fields"]:
        required.add(f"tushare:{row['api']}.{row['field']}")
    for dataset, fields in FIELD_REGISTRY.items():
        for api in (STANDARD_API_NAMES[dataset], VIP_API_NAMES.get(dataset)):
            if api:
                required.update(f"tushare:{api}.{field}" for field in fields)
    required.update("macro:"+s["id"] for s in read_json(root/"config/macro/macro-series.json")["series"])
    required.update("extension:"+s for s in read_json(root/"config/macro/macro-extensions.json")["groups"])
    required.update("ratio:"+s["id"] for s in read_json(root/"config/stocks/stock-ratio-catalog.json")["fields"])
    required.update(f"formula:{key}@{value.version}" for key,value in FORMULAS.items())
    required.update(f"card:{key}@{CARD_VERSION}" for key in DEFINITIONS)
    required.update(f"risk:{key}@{VERSION}" for key in RULES)
    required.update("spread:"+row[0] for row in SPREADS)
    required.update("screener:"+row[0] for row in REA_THRESHOLD_ROWS)
    required.add("source:cninfo-announcements")
    required.update("target:"+row["code"] for row in read_json(root/"config/macro/enterprise-targets.json")["targets"])
    for name,model in (("macro",MacroImportRecord),("content",ContentImportRecord)):
        required.update("import:"+name+"."+(f.alias or key) for key,f in model.model_fields.items())
    return required
