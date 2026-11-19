"""分红逐笔盘点。以请求发起前可用的原件为候选，正文相同仅作事后统计。"""

from collections import Counter, defaultdict
import json
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.connectors.tushare.contracts import request_hash
from app.ingestion.repositories import content_hash
from app.portfolio.dividend_fields import DIVIDEND_FIELDS
from app.portfolio.original_rows import _original_rows
from sqlalchemy import text


def records(s, sql):
    return list(map(dict, s.execute(text(sql)).mappings()))


def receipts(s):
    links, targets, plans = {}, {}, {}
    for job, key, value in s.execute(
        text(
            """
        select job_name,cursor_key,cursor_value from ops.job_cursor
        where job_name in ('sync-stock-data','portfolio-dividend-routine','portfolio-dividend-capture')
        order by job_name,cursor_key"""
        )
    ):
        identity = key.split(":")[0]
        if key.endswith(":target"):
            targets[identity] = value.get("asOf")
        if key.startswith("capture:") or key == "control":
            targets[value.get("identity")] = value.get("asOf")
        if key.startswith("plan:"):
            plans[key.split(":")[1]] = value
        if key.endswith("manifest:actions"):
            for p in value.get("plans", []):
                if p.get("api_name") == "dividend":
                    links.setdefault(("plan", identity, p["cursor_key"]), p)
        for attempt in [*value.get("previousAttempts", []), value]:
            if isinstance(attempt, dict) and attempt.get("batchId"):
                links[attempt["batchId"]] = (job, key, attempt)
    return links, targets, plans


KEY_FIELDS = ("ts_code", "end_date", "ann_date", "div_proc")
COMPARABLE_FIELDS = ("cash_div", "cash_div_tax", "record_date", "ex_date", "pay_date")
SUPPLEMENT_FIELDS = ("imp_ann_date", "base_date", "base_share")
NUMBER_FIELDS = {"cash_div", "cash_div_tax", "base_share", "stk_div", "stk_bo_rate", "stk_co_rate"}


def business_rows(raw_id, dataset, payload, stored_text):
    specialty = dataset == "portfolio_dividend_evidence"
    if "_http" in payload:
        fields = list(DIVIDEND_FIELDS) if specialty else payload["fields"]
        raw = SimpleNamespace(id=raw_id, payload=payload)
        rows = _original_rows(raw, fields, allow_more=not specialty)
        # sourceId 由验证器生成；调用方按同一顺序另记 rawId 和从 1 起算的行号。
        for row in rows:
            del row["sourceId"]
        return rows, "http-bytes-decimal"
    if specialty:
        raise ValueError(f"专项分红 Raw {raw_id} 缺少绑定的 HTTP 原字节")
    stored = json.loads(stored_text, parse_float=Decimal)
    return [dict(zip(stored["fields"], item, strict=True)) for item in stored["items"]], "stored-json-decimal"


def decimal_text(number):
    if not number.is_finite():
        raise ValueError("分红原件包含非有限数")
    if not number:
        return "0"
    formatted = format(number, "f")
    return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted


def business_rows_hash(rows):
    # 数字按十进制实际取值散列，字符串继续保留 JSON 类型。
    typed = [
        {
            field: {"number": decimal_text(Decimal(value))} if type(value) in (int, Decimal) else value
            for field, value in row.items()
        }
        for row in rows
    ]
    return content_hash(typed)


def event_value(field, value):
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    if field.endswith("_date") or field == "div_listdate":
        return str(value).replace("-", "")
    if field in NUMBER_FIELDS:
        return decimal_text(Decimal(str(value)))
    return str(value)


def event_key(row):
    return tuple(event_value(f, row[f]) or "" for f in KEY_FIELDS)


def compare_core_variants(core, variants):
    same, conflicting = [], set()
    for index, variant in enumerate(variants):
        fields = variant["fields"]
        different = {f for f in COMPARABLE_FIELDS if f not in fields or fields[f] != event_value(f, core[f])}
        if different:
            conflicting.update(different)
        else:
            same.append(index)
    if not same:
        agreement = "none"
    elif len(same) == len(variants):
        agreement = "all"
    else:
        agreement = "some"
    supplements = {}
    for f in SUPPLEMENT_FIELDS:
        if event_value(f, (core["data"] or {}).get(f)) is not None:
            continue
        values = {v["fields"].get(f) for v in variants}
        present = sorted(values - {None})
        if not present:
            continue
        if len(present) > 1:
            status = "different-values"
        elif agreement != "all":
            status = "core-mismatch"
        elif None in values:
            status = "missing-in-some"
        else:
            status = "confirmed"
        supplements[f] = {"status": status, "observedValues": present, "missingInSome": None in values}
        if status == "confirmed":
            supplements[f]["value"] = present[0]
    return {
        "id": core["id"],
        "comparableValues": {f: event_value(f, core[f]) for f in COMPARABLE_FIELDS},
        "agreement": agreement,
        "matchedVariants": same,
        "differentFields": sorted(conflicting),
        "supplements": supplements,
    }


def event_comparison(core_rows, observations):
    # 保留每条原始行的位置，按同键所有字段取值分组。字段清单不同也产生不同版本。
    sources = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for raw_id, row_number, record in observations:
        fields = {f: event_value(f, v) for f, v in record.items() if f not in KEY_FIELDS}
        variant = tuple(sorted(fields.items()))
        sources[event_key(record)][variant][raw_id].append(row_number)
    cores = defaultdict(list)
    for core in core_rows:
        cores[event_key(core)].append(core)
    agreement, diffs, confirmed, uncertain = Counter(), Counter(), Counter(), Counter()
    enriched = []
    within = between = 0
    evidence = []
    for key in sorted(sources.keys() | cores.keys()):
        variants = []
        per_capture = defaultdict(set)
        # None 与文本有明确的 JSON 排序，输入遍历顺序无关。
        for signature, refs in sorted(
            sources.get(key, {}).items(), key=lambda i: json.dumps(i[0], ensure_ascii=False)
        ):
            for raw_id in refs:
                per_capture[raw_id].add(len(variants))
            variants.append(
                {
                    "fields": dict(signature),
                    "sources": [{"rawId": raw_id, "rows": sorted(refs[raw_id])} for raw_id in sorted(refs)],
                }
            )
        multi = [raw_id for raw_id in sorted(per_capture) if len(per_capture[raw_id]) > 1]
        changed = len({frozenset(v) for v in per_capture.values()}) > 1
        within += bool(multi)
        between += changed
        compared = []
        for core in sorted(cores.get(key, []), key=lambda r: r["id"]):
            if not variants:
                compared.append({"id": core["id"], "agreement": "core-only"})
                continue
            comp = compare_core_variants(core, variants)
            agreement[comp["agreement"]] += 1
            diffs.update(comp["differentFields"])
            good = [f for f, item in comp["supplements"].items() if item["status"] == "confirmed"]
            confirmed.update(good)
            uncertain.update(f for f in comp["supplements"] if f not in good)
            if good:
                enriched.append(core["id"])
            compared.append(comp)
        evidence.append(
            {
                "key": dict(zip(KEY_FIELDS, key, strict=True)),
                "variants": variants,
                "multiVariantRawIds": multi,
                "variantSetChangedAcrossCaptures": changed,
                "core": compared,
            }
        )
    summary = {
        "coreRows": sum(map(len, cores.values())),
        "coreKeys": len(cores),
        "specialtyKeys": len(sources),
        "coreOnly": len(cores.keys() - sources.keys()),
        "specialtyOnly": len(sources.keys() - cores.keys()),
        "both": len(cores.keys() & sources.keys()),
        "coreVariantAgreement": dict(agreement),
        "intraCaptureVariantKeys": within,
        "crossCaptureChangedKeys": between,
        "conflicts": dict(diffs),
        "extraByField": dict(confirmed),
        "uncertainExtraByField": dict(uncertain),
        "enrichedCoreIds": sorted(enriched),
        "variantsFile": "dividend-event-evidence.jsonl",
    }
    return summary, evidence


def dividend_report(s, revision_days):
    captures = records(
        s,
        """select p.id,p.batch_id,p.request_hash,p.content_hash,cast(p.payload as text) as stored_payload,
        p.captured_at,b.source,b.dataset,
        b.status,b.mode,b.started_at,b.finished_at,b.request_count,b.row_count,b.rejected_count
        from raw.source_payload p join raw.ingestion_batch b on b.id=p.batch_id where p.api_name='dividend'
        order by p.captured_at,p.id""",
    )
    runs = records(
        s,
        """select id,job_name,business_key,started_at,finished_at,details from ops.job_run
        where job_name in ('sync-stock-data','backfill','backfill-universe')""",
    )
    links, targets, plans = receipts(s)
    symbols = set(s.scalars(text("select ts_code from core.security")))
    by_id, by_key, repeated, counts = {}, defaultdict(list), [], Counter()
    event_rows = []
    normalized_rows = {}
    precision_sources = defaultdict(Counter)
    for row in captures:
        stored = row.pop("stored_payload")
        body = json.loads(stored)
        row["hashVerified"] = content_hash(body) == row["content_hash"]
        fields, items = body["fields"], body["items"]
        business, method = business_rows(row["id"], row["dataset"], body, stored)
        normalized_rows[row["id"]] = business
        row["businessRowsSource"] = method
        precision_sources[row["dataset"]][method] += 1
        row["businessRowsHash"] = business_rows_hash(business)
        unit = links.get(row["batch_id"])
        if unit:
            job, key, attempt = unit
            identity = key.split(":")[0]
            row.update(
                cursorJob=job, cursorKey=key, target=targets.get(identity), unitStatus=attempt.get("status")
            )
            if p := links.get(("plan", identity, key.partition(":")[2])):
                row["params"] = p["params"]
                row["requestedFields"] = p["fields"]
            if attempt.get("symbol"):
                row["params"] = {"ts_code": attempt["symbol"]}
            if cached := attempt.get("cachedSourcePayloadId"):
                row["cachedSourcePayloadId"] = cached
        overlapping = [
            r for r in runs if r["finished_at"] and r["started_at"] <= row["started_at"] <= r["finished_at"]
        ]
        fields_requested = fields
        if len(overlapping) == 1 and overlapping[0]["job_name"] == "backfill-universe":
            plan = overlapping[0]["details"]
            window = {
                "start_date": str(plan.get("financialStart", "")).replace("-", ""),
                "end_date": str(plan.get("asOf", "")).replace("-", ""),
            }
            # 实际分页参数由历史请求哈希验证，配置值仅用于提出候选。
            sizes = {5000, 2000, 1000, 100}
            for sym in symbols:
                for size in sizes:
                    candidate = {"ts_code": sym, **window, "limit": str(size), "offset": "0"}
                    if request_hash("dividend", candidate, fields_requested) == row["request_hash"]:
                        row["params"] = candidate
                        break
                if "params" in row:
                    break
        if "params" not in row:
            # 即使空响应也可用证券池和字段清单验算逐股请求身份。
            match = [
                sym
                for sym in symbols
                if request_hash("dividend", {"ts_code": sym}, fields) == row["request_hash"]
            ]
            if len(match) == 1:
                row["params"] = {"ts_code": match[0]}
        row.setdefault("requestedFields", fields)
        row["paramsVerified"] = (
            "params" in row
            and request_hash("dividend", row["params"], row["requestedFields"]) == row["request_hash"]
        )
        row.update(
            responseFields=fields,
            receivedRows=len(items),
            hasMore=body.get("has_more"),
            hasTransport="_http" in body,
        )
        row["overlappingRuns"] = [
            {"id": r["id"], "job": r["job_name"], "businessKey": r["business_key"]}
            for r in runs
            if r["finished_at"] and r["started_at"] <= row["started_at"] <= r["finished_at"]
        ]
        key = (row["source"], row["request_hash"])
        prior = by_key[key]
        # 先按捕获顺序标出额外捕获；可复用判定只考虑已完成批次。
        equal = [p for p in prior if p["content_hash"] == row["content_hash"]]
        available = [p for p in prior if p["finished_at"] and p["finished_at"] <= row["started_at"]]
        if equal:
            last = available[-1] if available else {}
            cached = by_id.get(row.get("cachedSourcePayloadId"))
            decision, reason = "证据不足", "请求和正文相同，缺少当次任务允许沿用旧检查的证明"
            if overlapping and overlapping[0]["job_name"] == "backfill-universe":
                reason = "两次回补的证券池配置指纹不同；旧批次按池过滤后保留响应，缺少该页供应商原件和可跨池共享的完整性凭证"
            if (
                row["request_count"] == 0
                and cached
                and cached["status"] == "completed"
                and cached["rejected_count"] == 0
                and cached in available
                and cached["hashVerified"]
                and row["hashVerified"]
                and cached["content_hash"] == row["content_hash"]
                and row["paramsVerified"]
                and row["unitStatus"] == "completed"
            ):
                decision, reason = "可复用", "完成游标明确引用已存在的同请求原件，批次实发为零；历史已经复用"
            elif last and last["status"] != "completed":
                decision, reason = "应重新获取", "最近前次批次未成功，缺少当前任务可用的完整成功凭证"
            elif last and last["content_hash"] != row["content_hash"]:
                decision, reason = "应重新获取", "最近前次正文与本次不同，忽略较新版本回用更早同文会误选版本"
            elif last.get("target") and row.get("target") and row["target"] > last["target"]:
                decision, reason = "应重新获取", "新目标业务日需要复核公司行为，前次目标覆盖截止日较早"
            elif (
                row["dataset"] == "portfolio_dividend_evidence"
                and last
                and (row["started_at"] - last["captured_at"]) >= timedelta(days=revision_days)
                and unit
                and unit[0] == "portfolio-dividend-routine"
                and unit[1].split(":")[0] in plans
            ):
                decision, reason = "应重新获取", "组合例行计划已选入该证券，距前次专项凭证已达配置修订检查期"
            if row["request_count"] == 0 and decision != "可复用":
                decision, reason = "证据不足", "批次实发为零，无法据现存游标完整验证原件引用"
            row.update(
                decision=decision,
                reason=reason,
                previousEqualRawId=equal[-1]["id"],
                previousCompletedBeforeStart=next(
                    (p["id"] for p in reversed(available) if p["status"] == "completed"), None
                ),
                previousObservedBeforeStart=last.get("id"),
            )
            counts[decision] += 1
            repeated.append(row["id"])
        if row["dataset"] == "portfolio_dividend_evidence" and row["status"] == "completed":
            event_rows.extend(
                (row["id"], n, values) for n, values in enumerate(normalized_rows[row["id"]], 1)
            )
        prior.append(row)
        by_id[row["id"]] = row
    revised = [
        {
            "source": k[0],
            "requestHash": k[1],
            "rawIdsInOrder": [r["id"] for r in chain],
            "transitions": [
                [p["id"], n["id"]] for p, n in zip(chain, chain[1:]) if p["content_hash"] != n["content_hash"]
            ],
        }
        for k, chain in by_key.items()
        if len({r["content_hash"] for r in chain}) > 1
    ]
    for group in revised:
        details = []
        for before, after in group["transitions"]:
            old, new = normalized_rows[before], normalized_rows[after]

            def keyed(records):
                idx = defaultdict(set)
                for item in records:
                    k = tuple(str(item.get(f) or "") for f in ("ts_code", "end_date", "ann_date", "div_proc"))
                    idx[k].add(business_rows_hash([item]))
                return idx

            left, right = keyed(old), keyed(new)
            changes = sum(left[k] != right[k] for k in left.keys() & right.keys())
            if (
                by_id[before]["businessRowsHash"] == by_id[after]["businessRowsHash"]
                and by_id[before]["responseFields"] == by_id[after]["responseFields"]
            ):
                outcome = "解析字段与逐行业务值完全相同，差异在分页、封装或传输正文"
            elif Counter(business_rows_hash([r]) for r in old) == Counter(
                business_rows_hash([r]) for r in new
            ):
                outcome = "业务行集合相同，行序、字段顺序或封装变化"
            else:
                outcome = "解析后的业务行或字段发生变化"
            details.append(
                {
                    "before": before,
                    "after": after,
                    "classification": outcome,
                    "keysRemoved": len(left.keys() - right.keys()),
                    "keysAdded": len(right.keys() - left.keys()),
                    "keysWithChangedVariants": changes,
                }
            )
        group["comparison"] = details
    core = records(s, "select * from core.dividend order by ts_code,end_date,ann_date,div_proc,id")

    events, evidence = event_comparison(core, event_rows)
    return {
        "summary": {
            "captures": len(captures),
            "extraCaptures": len(repeated),
            "revisedKeys": len(revised),
            "businessRowsSources": {k: dict(v) for k, v in precision_sources.items()},
            "decisions": dict(counts),
            "physicalRequestsRecordedForExtraCaptures": sum(by_id[i]["request_count"] for i in repeated),
            "alreadyReused": sum(
                by_id[i]["decision"] == "可复用" and by_id[i]["request_count"] == 0 for i in repeated
            ),
            "provenAdditionalSavings": 0,
        },
        "events": events,
        "extraRawIds": repeated,
        "revisedRequests": revised,
        "captures": captures,
    }, evidence
