from __future__ import annotations

from app.stocks.derived.risks import RiskItem, evaluate_risks, score_risks
from app.shared.errors import DataServiceError


RISK_RULE_IDS = tuple(item.id for item in evaluate_risks({}).items)


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_item(item: object) -> None:
    text_fields = ("id", "title", "summary", "detail", "reportDate",
        "announcementDate", "reportType", "formula", "conclusion")
    if not isinstance(item, dict) or not all(isinstance(item.get(field), str) for field in text_fields):
        raise DataServiceError("INVALID_SOURCE_PAYLOAD", "风险项不符合契约")
    inputs = item.get("inputs")
    if (item.get("status") not in ("normal", "risk", "unknown", "watch")
        or not _strings(item.get("missingFields"))
        or not isinstance(inputs, list)
        or not all(isinstance(entry, dict) and isinstance(entry.get("label"), str)
            and isinstance(entry.get("value"), str) for entry in inputs)):
        raise DataServiceError("INVALID_SOURCE_PAYLOAD", "风险项明细不符合契约")
    if "drilldown" in item:
        target = item["drilldown"]
        if (not isinstance(target, dict) or target.get("tab") != "statements"
            or target.get("statement") not in ("income", "balance", "cashflow")
            or not _strings(target.get("fields"))):
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "风险透视目标不符合契约")


def assess_risk_checklist(value: object) -> dict[str, object]:
    """仅按快照明细生成展示摘要，不查询当前输入或修改逐项结论。"""
    if isinstance(value, dict) and "ruleVersion" in value:
        return assess_current(value)
    if not isinstance(value, dict) or not isinstance(value.get("groups"), list):
        raise DataServiceError("INVALID_SOURCE_PAYLOAD", "风险明细不符合契约")
    statuses: dict[str, str] = {}
    for group in value["groups"]:
        if (not isinstance(group, dict) or not isinstance(group.get("items"), list)
            or group.get("id") not in ("operations", "dividend")
            or not all(isinstance(group.get(field), str) for field in ("title", "description"))):
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "风险分组不符合契约")
        for item in group["items"]:
            _validate_item(item)
            key = item["id"]
            if key not in RISK_RULE_IDS:
                raise DataServiceError("INVALID_SOURCE_PAYLOAD", "风险规则未识别")
            if key in statuses:
                raise DataServiceError("INVALID_SOURCE_PAYLOAD", "风险项重复")
            statuses[key] = item["status"]
    records = tuple(RiskItem(key, statuses.get(key, "unknown"), "", {}, (), "") for key in RISK_RULE_IDS)
    summary = score_risks(records)
    known = sum(item.status in ("normal", "risk") for item in records)
    risk_count = sum(item.status == "risk" for item in records)
    unknown = len(records) - known
    base_level = "高风险" if risk_count >= 3 else "需关注" if risk_count else "正常"
    if summary.score is None:
        level = "数据不足" if not risk_count else "数据不足（已发现高风险）" if risk_count >= 3 else "数据不足（已发现风险）"
    elif unknown:
        level = "部分覆盖" if not risk_count else f"{base_level}（部分覆盖）"
    else:
        level = base_level
    return {**value, "score": float(summary.score) if summary.score is not None else None,
        "level": level, "assessment": {"coverage": float(summary.coverage), "knownCount": known,
            "totalCount": len(records), "unknownCount": unknown,
            "missingRuleIds": [key for key in RISK_RULE_IDS if key not in statuses],
            "policyVersion": "risk-coverage-v1"}}


def assess_current(value):
    from app.stocks.derived.workbench_risks import RULES, VERSION

    version = value.get("ruleVersion")
    if version not in ("workbench-risk-v2", VERSION):
        raise DataServiceError("INVALID_SOURCE_PAYLOAD", "未知风险规则版本")
    groups = value.get("groups", [])
    if [g.get("id") for g in groups] != ["operations", "dividend"]:
        raise DataServiceError("INVALID_SOURCE_PAYLOAD", "风险分组不完整")
    items = [item for group in groups for item in group["items"]]
    if len(groups[0]["items"]) != 7 or [item["id"] for item in items] != list(RULES):
        raise DataServiceError("INVALID_SOURCE_PAYLOAD", "风险规则不完整")
    for item in items:
        _validate_item(item)
        if not _strings(item.get("steps")) or not isinstance(item.get("threshold"), str):
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "风险计算依据不完整")
    statuses = [item["status"] for item in items]
    unknown = statuses.count("unknown")
    score = max(0, 100 - 18 * statuses.count("risk") - 10 * statuses.count("watch") - 4 * unknown)
    level = "稳健" if score >= 85 else "中性" if score >= 70 else "谨慎" if score >= 55 else "高风险"
    return {**value, "score": score if unknown <= 4 else None, "level": level if unknown <= 4 else "数据不足",
            "assessment": dict(coverage=(12 - unknown) / 12, knownCount=12 - unknown, totalCount=12,
                               unknownCount=unknown, missingRuleIds=[], policyVersion=version)}
