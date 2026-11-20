"""工作台股息率的版本化、只读证据投影。"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
import hmac
import json
import re
import secrets

from sqlalchemy import String, cast, func, select

from app.connectors.tushare.contracts import request_hash
from app.db.models import IngestionBatch, MetricInput, MetricValue, SourcePayload
from app.ingestion.repositories import content_hash
from app.portfolio.api.dividends import original_dividend_rows
from app.shared.errors import DataServiceError
from app.shared.repository import SnapshotRepository
from app.stocks.derived.workbench_cards import DEFINITIONS, metric_value_card


KINDS = {
    "workbenchFiscalDividendYield": "fact",
    "workbenchAnnualDividendYield": "estimate",
    "workbenchNtmDividendYield": "estimate",
}
_SIGNING_KEY = secrets.token_bytes(32)
_MAX_SOURCES = 128
_MAX_CALCULATION_BYTES = 32 * 1024
_MAX_RESULT_BYTES = 96 * 1024
_MAX_ORIGINAL_CHARS = 1024 * 1024
_MAX_ORIGINALS = 12
_RAW_REF = re.compile(r"raw\.source_payload:([1-9][0-9]{0,9})(?::row:([1-9][0-9]{0,8}))?")
_DATASETS = {"dividend": {"dividend", "portfolio_dividend_evidence"},
             "income": {"financial-statement-evidence"}}
_FIELDS = {
    "dividend": ("ts_code", "end_date", "ann_date", "imp_ann_date", "div_proc", "record_date",
                 "ex_date", "pay_date", "cash_div_tax", "base_share"),
    "income": ("ts_code", "end_date", "ann_date", "f_ann_date", "report_type", "update_flag",
               "n_income_attr_p", "basic_eps"),
}
_UNITS = {"cash_div_tax": "元/股", "base_share": "万股", "n_income_attr_p": "元", "basic_eps": "元/股"}


def _bounded(value, limit):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > limit:
        raise DataServiceError("INVALID_REQUEST", "所选证据超出单次响应范围")
    return encoded


def _selected_cards(symbol, version, data, selected_metrics):
    if data.get("symbol") != symbol:
        raise DataServiceError("DATA_NOT_FOUND", "工作台与所选证券不符")
    selected = set(KINDS if selected_metrics is None else selected_metrics)
    if not selected <= KINDS.keys():
        raise DataServiceError("INVALID_REQUEST", "该指标不在受管证据目录中")
    seen = set()
    for card in data.get("metrics", {}).get("cards", []):
        if not isinstance(card, dict) or card.get("id") not in selected:
            continue
        metric = card["id"]
        if metric in seen:
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "工作台含重复的规范指标")
        seen.add(metric)
        calc = card.get("calculation") or {}
        if not isinstance(calc, dict):
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "指标计算说明格式有误")
        refs = calc.get("sourceIds", [])
        if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "指标来源引用格式有误")
        if len(refs) > _MAX_SOURCES:
            raise DataServiceError("INVALID_REQUEST", "所选指标来源数量超过上限")
        binding = _bounded([symbol, version, card], _MAX_CALCULATION_BYTES)
        ref = f"ev1-{metric}-" + hmac.digest(_SIGNING_KEY, binding, "sha256").hex()
        yield card, dict(evidenceRef=ref, metricId=metric, kind=KINDS[metric],
                         displayValue=card.get("value"), formulaVersion=calc.get("formulaVersion"))


def evidence_catalog(symbol: str, version: str, data: dict, selected_metrics: list[str] | None = None) -> list[dict]:
    return [entry for _, entry in _selected_cards(symbol, version, data, selected_metrics)]


def _gap(code, message, source=None):
    return {"code": code, "message": message, **({"sourceId": source} if source else {})}


def _derived(session, result, day, card):
    calc = result["calculation"]
    metric = result["metricId"]
    if calc.get("asOf") != day.isoformat() or not result["formulaVersion"]:
        result["gaps"].append(_gap("METRIC_PERIOD_UNVERIFIED", "指标日期或公式版本与快照无法核对"))
        return
    # 同一 (证券、业务日、公式版本) 可以重算。单条语句读取派生值与它的输入记录。
    records = session.execute(select(MetricValue, MetricInput).outerjoin(
        MetricInput, MetricInput.metric_value_id == MetricValue.id
    ).where(MetricValue.ts_code == result["symbol"], MetricValue.as_of == day,
            MetricValue.formula_version == result["formulaVersion"], MetricValue.metric_id == metric)
      .order_by(MetricInput.id).limit(_MAX_SOURCES + 3)).all()
    if len(records) > _MAX_SOURCES + 1:
        raise DataServiceError("INVALID_REQUEST", "该指标派生输入数量超过上限")
    record = records[0][0] if records else None
    if (record is None or record.calculation != calc or record.unit != result["unit"]
            or metric_value_card(record)["value"] != card.get("value")):
        result["gaps"].append(_gap("DERIVED_VERSION_UNVERIFIED", "未找到与本版快照一致的派生指标记录"))
        return
    # v2 把 Numeric(28,10) 同时存入末行；拦住显示精度相同、底层值发生变化的修订。
    if record.formula_version == "v2" and record.value is not None:
        final_row = (calc.get("rows") or [{}])[-1]
        exact_forms = {f"{record.value} {record.unit}", f"{record.value:f} {record.unit}"}
        if final_row.get("label") != "计算结果" or final_row.get("value") not in exact_forms:
            result["gaps"].append(_gap("DERIVED_VERSION_UNVERIFIED", "原精度结果与本版快照不符"))
            return
    result["sourceId"] = f"derived.metric_value:{record.id}"
    result["value"] = format(record.value, "f") if record.value is not None else None
    if result["value"] is None:
        result["gaps"].append(_gap("VALUE_MISSING", "指标无可用原精度数值"))
    allowed = set(calc.get("sourceIds", []))
    verified = set()
    for _, item in records:
        if item is None:
            continue
        source = f"{item.source_table}:{item.source_record_key}"
        if (item.input_name == "source" and source in allowed) or (
                item.input_name == "calculation" and source == f"derived.workbench_calculation:{metric}"):
            result["inputs"].append({"sourceId": source, "contentHash": item.content_hash, "name": item.input_name})
            verified.add(source)
    if not result["inputs"] or allowed - verified:
        result["gaps"].append(_gap("INPUTS_UNVERIFIED", "部分指标输入未绑定到派生记录"))


def _rows(raw, fields):
    if "_http" in raw.payload:
        return original_dividend_rows(raw, fields, allow_more=True)
    rows = raw.payload["items"]
    if not isinstance(rows, list):
        raise ValueError
    return [dict(zip(fields, values, strict=True)) for values in rows]


def _original(session, raw_id, wanted, symbol, gaps):
    source = f"raw.source_payload:{raw_id}"
    entry = session.execute(select(SourcePayload, IngestionBatch).join(
        IngestionBatch, IngestionBatch.id == SourcePayload.batch_id
    ).where(SourcePayload.id == raw_id, IngestionBatch.source == "tushare",
            IngestionBatch.status == "completed", IngestionBatch.rejected_count == 0,
            SourcePayload.api_name.in_(_DATASETS),
            func.length(cast(SourcePayload.payload, String)) <= _MAX_ORIGINAL_CHARS)).one_or_none()
    if not entry:
        gaps.append(_gap("ORIGINAL_UNAVAILABLE", "原件缺失、未获准或超出读取范围", source))
        return None
    raw, batch = entry
    if batch.dataset not in _DATASETS[raw.api_name] or content_hash(raw.payload) != raw.content_hash:
        gaps.append(_gap("ORIGINAL_UNVERIFIED", "原件数据集或内容摘要无法核对", source))
        return None
    try:
        fields = raw.payload["fields"]
        if not isinstance(fields, list) or "ts_code" not in fields or len(set(fields)) != len(fields):
            raise ValueError
        rows = _rows(raw, fields)
    except (KeyError, TypeError, ValueError):
        gaps.append(_gap("ORIGINAL_UNVERIFIED", "原件行格式或正文未通过核对", source))
        return None
    projected = []
    # 仅指向查询批次的引用，需有与所选证券匹配的单证券查询。整批记录不对外返回。
    bound = (None in wanted and raw.request_hash == request_hash(raw.api_name, {"ts_code": symbol}, fields)
             and all(row.get("ts_code") == symbol for row in rows))
    if None in wanted and not bound:
        gaps.append(_gap("ORIGINAL_SCOPE_UNVERIFIED", "无法核对批次引用的单证券范围", source))
    for number in sorted(index for index in wanted if index is not None):
        row_ref = f"{source}:row:{number}"
        row = rows[number - 1] if number <= len(rows) else {}
        if row.get("ts_code") != symbol:
            gaps.append(_gap("ORIGINAL_ROW_UNVERIFIED", "原件行与所选证券无法核对", row_ref))
            continue
        values = {k: str(row[k]) if isinstance(row[k], Decimal) else row[k]
                  for k in _FIELDS[raw.api_name] if k in row}
        if any(isinstance(v, (dict, list)) for v in values.values()):
            gaps.append(_gap("ORIGINAL_ROW_UNVERIFIED", "原件行字段格式有误", row_ref))
            continue
        projected.append(dict(sourceId=row_ref, fields=values))
    if not bound and not projected:
        return None
    return dict(sourceId=source, source=batch.source, apiName=raw.api_name, dataset=batch.dataset,
                contentHash=raw.content_hash, capturedAt=raw.captured_at.isoformat() if raw.captured_at else None,
                rows=projected, fieldUnits={k: _UNITS[k] for k in _FIELDS[raw.api_name] if k in _UNITS and k in fields})


def expand_evidence(session_factory, symbol: str, version: str, evidence_ref: str) -> dict:
    parts = re.fullmatch(r"ev1-([A-Za-z]{1,40})-[0-9a-f]{64}", evidence_ref) if isinstance(evidence_ref, str) else None
    if not parts or parts[1] not in KINDS:
        raise DataServiceError("DATA_NOT_FOUND", "未登记的证据引用")
    snapshot = SnapshotRepository(session_factory).read_versioned(f"stock-workbench:{symbol}", version)
    selected = list(_selected_cards(symbol, version, snapshot.raw, [parts[1]]))
    if not selected or not hmac.compare_digest(selected[0][1]["evidenceRef"], evidence_ref):
        raise DataServiceError("DATA_NOT_FOUND", "证据引用与所选证券、版本或卡片不符")
    card, ref = selected[0]
    calc = card.get("calculation") or {}
    gaps = []
    result = dict(**ref, symbol=symbol, snapshotVersion=version, asOf=snapshot.meta.as_of,
                  calculation=calc, unit=DEFINITIONS[ref["metricId"]][1], value=None, sourceId=None,
                  inputs=[], originals=[], gaps=gaps)
    by_original = defaultdict(set)
    for source in calc.get("sourceIds", []):
        match = _RAW_REF.fullmatch(source)
        if match:
            raw_id, row = match.groups()
            by_original[int(raw_id)].add(int(row) if row else None)
        elif source.startswith("core.daily_price:"):
            gaps.append(_gap("PRICE_ORIGINAL_UNVERIFIED", "快照仅绑定了 Core 行情记录，具体行情原件和获取时间未核实", source))
    if len(by_original) > _MAX_ORIGINALS:
        raise DataServiceError("INVALID_REQUEST", "该指标原件数量超出单次展开范围")
    if not by_original:
        gaps.append(_gap("ORIGINAL_NOT_REFERENCED", "快照没有可展开的受管原件引用"))
    with session_factory() as session:
        _derived(session, result, date.fromisoformat(snapshot.meta.as_of[:10]), card)
        for raw_id, wanted in by_original.items():
            original = _original(session, raw_id, wanted, symbol, gaps)
            if original is not None:
                result["originals"].append(original)
    _bounded(result, _MAX_RESULT_BYTES)
    return result
