"""工作台发布时验证风险输入，并将当前十二卡写入同一事务。"""
from collections import defaultdict
from dataclasses import replace

from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from app.db.models import PublishedSnapshot, RiskResult
from app.ingestion.repositories import content_hash
from app.marts.publication import PublicationBaseChanged
from app.stocks.derived.risk_inputs import load_risk_inputs
from app.stocks.derived.workbench_risks import VERSION, calculate_risks


def history_payload(product, payload):
    if product.startswith("stock-workbench:") and "risks" in payload:
        return {key: value for key, value in payload.items() if key != "risks"}
    return payload


def project_risks(session, candidates):
    groups = defaultdict(list)
    for candidate in candidates:
        groups[candidate.as_of.date()].append(candidate)
    result = []
    for day, group in groups.items():
        inputs = load_risk_inputs(session, [c.payload["symbol"] for c in group], day)
        for candidate in group:
            symbol = candidate.payload["symbol"]
            if candidate.product != "stock-workbench:" + symbol:
                raise ValueError("风险计算证券不匹配")
            source = inputs[symbol]
            risks = calculate_risks(source)
            payload = candidate.payload | {"risks": risks}
            result.append(replace(candidate, payload=payload, risk_input_signature=content_hash([VERSION, source]),
                                  rows=replace(candidate.rows, workbenches=(payload,))))
    return tuple(result)


def project_overlay_risks(session, candidates, versions):
    workbenches = [replace(c, workbench_parent_version=versions[c.product])
                   for c in candidates if c.product.startswith("stock-workbench:")
                   and c.payload.get("risks", {}).get("ruleVersion")]
    projected = {c.product: c for c in project_risks(session, workbenches)}
    return tuple(projected.get(c.product, c) for c in candidates)


def verify_risk_sources(session, candidates):
    builds = [candidate for candidate in candidates if candidate.risk_input_signature]
    if not builds:
        return
    try:
        session.execute(text("LOCK TABLE core.income_statement, core.balance_sheet, core.cashflow_statement, "
                             "core.financial_indicator, core.dividend, core.daily_basic, core.daily_price, raw.source_payload, "
                             "raw.ingestion_batch, ops.job_cursor IN SHARE MODE NOWAIT"))
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            from app.stocks.marts.metric_cards import MetricCardSourceBusy
            raise MetricCardSourceBusy("WORKBENCH_RISK_SOURCE_BUSY") from exc
        raise
    for candidate, verified in zip(sorted(builds, key=lambda c: c.product),
                                   sorted(project_risks(session, builds), key=lambda c: c.product), strict=True):
        parent = session.get(PublishedSnapshot, candidate.product, with_for_update=True)
        if ((parent.version if parent else None) != candidate.workbench_parent_version
                or candidate.risk_input_signature != verified.risk_input_signature
                or candidate.payload["risks"] != verified.payload["risks"]):
            raise PublicationBaseChanged("WORKBENCH_RISK_SOURCE_CHANGED")


def persist_current_risks(session, current):
    risks = current.payload.get("risks", {})
    if risks.get("ruleVersion") != VERSION:
        return
    symbol = current.product.split(":", 1)[1]
    rows = list(session.scalars(select(RiskResult).where(RiskResult.ts_code == symbol)))
    items = [item for group in risks["groups"] for item in group["items"]]
    if len(rows) == len(items) and {row.risk_id: row.inputs for row in rows} == {i["id"]: i for i in items}:
        for row in rows:
            row.snapshot_version, row.rule_version, row.as_of = current.version, VERSION, current.as_of.date()
        return
    session.execute(delete(RiskResult).where(RiskResult.ts_code == symbol))
    session.add_all(RiskResult(ts_code=symbol, risk_id=i["id"], as_of=current.as_of.date(),
        snapshot_version=current.version, rule_version=VERSION, status=i["status"], formula=i["formula"],
        conclusion=i["conclusion"], inputs=i, missing_fields=i["missingFields"]) for i in items)
