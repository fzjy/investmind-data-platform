"""按业务日使用生产只读装载器与公式核对每条派生结果及全部输入签名。"""
import argparse
from collections import Counter, defaultdict
from importlib import import_module
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import sys

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import settings
from app.db.models import MetricInput, MetricValue
from app.shared.readonly_database import open_readonly_factory


def fingerprint(name, source):
    return (name, source.source_table, source.source_record_key, source.content_hash)


def compare(value, references, row, expected):
    rounded = value.quantize(Decimal("1e-10"), rounding=ROUND_HALF_UP) if value is not None else None
    same_value = row.value == rounded
    same_sources = Counter(references) == expected
    if same_value and same_sources:
        return "valueAndAllInputsMatch"
    return "valueOnlyMatches" if same_value else "inputsOnlyMatch" if same_sources else "valueAndInputsDiffer"


def replay(archive_root=None, pending=None):
    if archive_root:
        # 已取得当前只读工厂和查询模型；公式及输入装载使用对应归档的模块。
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.insert(0, str(archive_root / "backend"))
    code_root = archive_root or ROOT
    namespace = "app.stocks.derived" if (code_root / "backend/app/stocks/derived").is_dir() else "app.derived"
    inputs_module = import_module(namespace + ".inputs")
    registry = import_module(namespace + ".registry")
    cards_module = (import_module(namespace + ".workbench_cards")
                    if (code_root / "backend" / namespace.replace(".", "/") / "workbench_cards.py").exists() else None)
    pending_keys = None if pending is None else {
        (entry["metric"], entry["formulaVersion"], entry["asOf"]) for entry in pending["metricVersionDates"]
        if entry.get("archivedFormulaRequired", 0)
    }
    results = defaultdict(Counter)
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        by_day = defaultdict(list)
        for row in session.scalars(select(MetricValue)):
            if pending_keys is None or (row.metric_id, row.formula_version, str(row.as_of)) in pending_keys:
                by_day[row.as_of].append(row)
        signatures = defaultdict(Counter)
        for ref in session.scalars(select(MetricInput)):
            signatures[ref.metric_value_id][(ref.input_name, ref.source_table, ref.source_record_key, ref.content_hash)] += 1
        for day, rows in sorted(by_day.items()):
            symbols = tuple(sorted({row.ts_code for row in rows}))
            snapshot = inputs_module.CoreSnapshotRepository(factory).load(day, symbols)
            card_symbols = {row.ts_code for row in rows if cards_module and row.formula_version == cards_module.CARD_VERSION}
            cards = {}
            manifest_gap = False
            if card_symbols:
                try:
                    card_inputs = inputs_module.load_metric_card_inputs(session, card_symbols, day)
                except ValueError as error:
                    if not archive_root or str(error) != "DIVIDEND_MANIFEST_INVALID":
                        raise
                    manifest_gap = True
                    card_inputs = {}
                for symbol, inputs in card_inputs.items():
                    for card in cards_module.calculate_metric_cards(inputs):
                        cards[symbol, card.metric_id] = card
            for row in rows:
                count = results[(row.metric_id, row.formula_version, str(day))]
                definition = registry.FORMULAS.get(row.metric_id)
                if definition and definition.version == row.formula_version:
                    context = snapshot.security_inputs.get(row.ts_code)
                    if context is None:
                        count["missingSecurityInputs"] += 1
                        continue
                    value = registry.calculate_formula(row.metric_id, context.values).value
                    refs = []
                    for name in definition.inputs:
                        source = context.sources.get(name, inputs_module.InputSource("input_context", f"{row.ts_code}:{day}:{name}", context.values.get(name)))
                        refs.append(fingerprint(name, source))
                elif manifest_gap and row.formula_version == cards_module.CARD_VERSION:
                    count["archivedDividendManifestRejected"] += 1
                    continue
                elif (row.ts_code, row.metric_id) in cards and row.formula_version == cards_module.CARD_VERSION:
                    card = cards[row.ts_code, row.metric_id]
                    value = card.value
                    refs = []
                    for key, evidence in card.sources.items():
                        table, _, record_key = key.partition(":")
                        refs.append(fingerprint("source", inputs_module.InputSource(table, record_key, evidence)))
                    refs.append(fingerprint("calculation", inputs_module.InputSource("derived.workbench_calculation", row.metric_id, card.calculation())))
                else:
                    count["archivedFormulaRequired"] += 1
                    continue
                count[compare(value, refs, row, signatures[row.id])] += 1
    total = sum(results.values(), Counter())
    return {"summary": dict(total), "metricVersionDates": [
        {"metric": key[0], "formulaVersion": key[1], "asOf": key[2], **counts}
        for key, counts in sorted(results.items())
    ]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path)
    parser.add_argument("--pending", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/research/investmind-metric-replay.json")
    args = parser.parse_args()
    pending = json.loads(args.pending.read_text(encoding="utf8")) if args.pending else None
    report = replay(args.archive_root, pending)
    path = args.output
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
    print(report["summary"])
