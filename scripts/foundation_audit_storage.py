"""关系、约束、跨表事实、表内镜像与采集重叠。"""

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re

from sqlalchemy import text

MANUAL_KEYS = {
    "core.repurchase": ["ts_code", "ann_date", "proc"],
    "core.share_unlock": ["ts_code", "ann_date"],
    "core.top_holder": ["ts_code", "end_date", "holder_name"],
    "core.business_segment": ["ts_code", "end_date", "segment_name"],
    "core.security_name_history": ["ts_code", "start_date", "end_date", "name"],
    "core.listing_status_history": ["ts_code", "effective_from", "status"],
}
DISPOSITIONS = {
    "core": "标准事实与领域选版依据；保留可追溯的来源引用和历史字段",
    "raw": "内部采集原件、文件、失败和批次证据；保持捕获身份与留存职责",
    "derived": "InvestMind 计算结果、公式版本及可重放血缘；独立于原始事实保存",
    "mart": "InvestMind 发布及历史重放；保持版本、摘要和回退读取",
    "ops": "内部覆盖、任务、治理或运维；保留执行及查询凭证",
    "public": "内部数据库迁移版本",
}


def quote(name):
    if not re.fullmatch(r"[a-z_][a-z_0-9]*", name):
        raise ValueError(name)
    return '"' + name + '"'


def compare(field_type, typed, raw):
    if typed is None or raw is None:
        return "bothNull" if typed is None and raw is None else "oneNull"
    if field_type in ("numeric", "integer", "bigint", "smallint", "double precision", "real"):
        try:
            left, right = Decimal(str(typed)), Decimal(str(raw))
        except InvalidOperation:
            return "invalidNumeric"
        if left == right:
            return "equalZero" if left == 0 else "equal"
        return "different"
    if field_type == "date":
        if isinstance(raw, (int, str)) and re.fullmatch(r"[0-9]{8}", str(raw)):
            raw = str(raw)
            raw = raw[:4] + "-" + raw[4:6] + "-" + raw[6:]
        return "equal" if str(typed) == raw else "different"
    return "equal" if typed == raw else "different"


def mirror_stats(s, table, types):
    values = [name for name in types if name not in ("data", "id")]
    summary = {col: Counter() for col in values}
    examples = {}
    result = s.execute(text("select " + ",".join(map(quote, values)) + ",data::text from " + table))
    for record in result:
        obj = json.loads(record[-1], parse_float=Decimal) if record[-1] else {}
        for col, typed in zip(values, record):
            outcome = compare(types[col], typed, obj[col]) if col in obj else "absentFromJson"
            summary[col][outcome] += 1
            if outcome in ("different", "invalidNumeric", "oneNull"):
                examples.setdefault(col, {"typed": typed, "json": obj[col]})
    return {"columns": summary, "firstDifference": examples}


def storage(s, rows, schemas):
    objects = rows(
        s,
        """select n.nspname as schema,c.relname as name,c.relkind as kind
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where n.nspname=any(:schemas) and c.relkind in ('r','v','m','p','f') order by 1,2""",
        schemas=list(schemas),
    )
    allcols = rows(
        s,
        """select table_schema||'.'||table_name as rel,column_name as name,data_type,
        numeric_precision,numeric_scale,is_nullable from information_schema.columns where table_schema=any(:schemas)
        order by 1,ordinal_position""",
        schemas=list(schemas),
    )
    cons = rows(
        s,
        """select c.conrelid::regclass::text rel,c.conname name,c.contype as type,
        pg_get_constraintdef(c.oid) as definition,array(select a.attname from unnest(c.conkey)
        with ordinality k(attnum,i) join pg_attribute a on a.attrelid=c.conrelid and a.attnum=k.attnum order by k.i) as cols
        from pg_constraint c join pg_namespace n on n.oid=c.connamespace where n.nspname=any(:schemas) order by 1,2""",
        schemas=list(schemas),
    )
    inds = rows(
        s,
        "select schemaname||'.'||tablename rel,indexdef from pg_indexes where schemaname=any(:schemas) order by 1,indexname",
        schemas=list(schemas),
    )
    for obj in objects:
        schema, name = obj["schema"], obj["name"]
        rel = schema + "." + name
        table = quote(schema) + "." + quote(name)
        obj["rel"] = rel
        obj["disposition"] = DISPOSITIONS[schema]
        obj["columns"] = [
            {k: v for k, v in c.items() if k != "rel" and v is not None} for c in allcols if c["rel"] == rel
        ]
        obj["indexes"] = [r["indexdef"] for r in inds if r["rel"] == rel]
        obj["constraints"] = [
            {k: v for k, v in c.items() if k != "rel"}
            for c in cons
            if c["rel"] == rel or schema == "public" and c["rel"] == name
        ]
        obj["rows"] = s.scalar(text(f"select count(*) from {table}"))
        uq = [c["cols"] for c in obj["constraints"] if c["type"] == "u"]
        pk = [c["cols"] for c in obj["constraints"] if c["type"] == "p"]
        key = MANUAL_KEYS.get(rel) or next(iter(uq or pk), [])
        obj["keyChecked"] = key
        if key:
            cols = ",".join(map(quote, key))
            # 即使唯一约束包含 NULL 也按业务值成组；逐表披露缺维度的重复。
            obj["extraByKey"] = s.scalar(
                text(
                    f"select coalesce(sum(n-1),0)::bigint from (select count(*) n from {table} group by {cols} having count(*)>1) g"
                )
            )
        types = {r["name"]: r["data_type"] for r in obj["columns"]}
        if schema == "core" and "data" in types:
            obj["mirror"] = mirror_stats(s, table, types)
    return objects


def cross_checks(s, rows, snapshots):
    queries = {
        "dailyPriceAndFactor": """select count(*) filter(where p.id is null) as factor_only,count(*) filter(where f.id is null) as price_only,
          count(*) filter(where p.id is not null and f.id is not null) as both from core.daily_price p full join core.adjustment_factor f using(ts_code,trade_date)""",
        "dailyPriceAndBasic": """select count(*) filter(where p.id is null) as basic_only,count(*) filter(where b.id is null) as price_only,
          count(*) filter(where p.id is not null and b.id is not null) as both from core.daily_price p full join core.daily_basic b using(ts_code,trade_date)""",
        "marketQuoteSource": """select q.api_name,count(*) as rows,count(*) filter(where p.id is null) as missing_raw,
          count(*) filter(where p.api_name is distinct from q.api_name or q.captured_at is distinct from p.captured_at) as different_source_metadata
          from core.market_daily_quote q left join raw.source_payload p on p.id=q.source_payload_id group by 1 order by 1""",
        "macroSources": """with g as (select series_id,observed_at,unit,frequency,count(distinct source) sources,count(distinct value) as vals,
          count(*) n from core.macro_observation group by 1,2,3,4) select series_id,sum(n)::int as rows,count(*) as periods,
          count(*) filter(where sources>1) multi_source_periods,count(*) filter(where vals>1) as multi_value_periods,
          count(*) filter(where n>1) multi_version_periods from g group by 1 order by 1""",
        "newsDeliveredDocuments": """with d as (select source,external_id,title,published_at from core.content_document where content_type='news'),
          n as (select source,external_id,delivered,payload from ops.news_delivery)
          select coalesce(n.source,d.source) source,count(*) filter(where d.source is null) as delivery_only,
          count(*) filter(where n.source is null) as core_only,count(*) filter(where n.source is not null and d.source is not null) as both,
          count(*) filter(where d.title=n.payload->>'title') equal_title,
          count(*) filter(where d.title is distinct from n.payload->>'title' and n.source is not null and d.source is not null) different_title
          from n full join d using(source,external_id) group by 1 order by 1""",
        "newsIdentities": """with d as(select source,external_id,count(*) n from core.content_document where content_type='news' group by 1,2),
          n as(select source,external_id,count(*) n,count(distinct payload->>'title') titles from ops.news_delivery group by 1,2)
          select coalesce(n.source,d.source) source,count(*) filter(where n.source is null) as only_core_keys,count(*) filter(where d.source is null) as only_delivery_keys,
          count(*) filter(where n.source is not null and d.source is not null) matching_keys,sum(n.n)::bigint deliveries,sum(d.n)::bigint documents,
          count(*) filter(where n.n>1) as repeated_delivery_keys,count(*) filter(where n.titles>1) as revised_titles from d full join n using(source,external_id) group by 1 order by 1""",
        "externalFileRecords": """select f.schema_version,f.status,count(distinct f.id) files,sum(r.n)::bigint retained_records,
          count(*) filter(where f.row_count is distinct from r.n) declared_vs_retained_diff from raw.external_file f
          left join lateral(select count(*) as n from raw.external_record where external_file_id=f.id) r on true group by 1,2 order by 1,2""",
        "publicationBodies": f"""select p.product,p.version,p.as_of,p.published_at,p.stale,
          encode(sha256(convert_to(p.payload::text,'UTF8')),'hex') as sha256,
          h.id as history_id,json_typeof(o.payload)='string' as references_history,
          p.payload::jsonb=h.payload::jsonb as same_history
          from {snapshots} p join mart.published_snapshot o on o.product=p.product left join mart.page_snapshot h
          on h.product=p.product and h.snapshot_version=p.version order by 1""",
        "sharesAndUniqueReceipts": """with by_body as(select b.source,p.api_name,p.request_hash,p.content_hash,
          count(*) n,min(p.id) sample, array_agg(distinct b.dataset order by b.dataset) groups from raw.source_payload p
          join raw.ingestion_batch b on b.id=p.batch_id group by 1,2,3,4),
          by_req as(select source,api_name,request_hash,sum(n)::int n,count(*) variants,min(sample) sample from by_body group by 1,2,3)
          select source,api_name,sum(n)::int captures,count(*) request_keys,sum(n-variants)::int extra_captures,
          count(*) filter(where variants>1) revised_keys,min(sample) as example_id from by_req group by 1,2 order by 1,2""",
        "rawTaskOwnership": """select b.source,p.api_name,b.dataset,b.mode,b.status,count(*) captures,min(p.id) sample
            from raw.source_payload p join raw.ingestion_batch b on b.id=p.batch_id group by 1,2,3,4,5 order by 1,2,3,4,5""",
        "actualRuns": """select job_name,status,count(*) as runs,min(started_at),max(started_at),
            sum(request_count) as recorded_requests from ops.job_run group by 1,2 order by 1,2""",
        "cursorJobs": """select job_name,count(*) as units from ops.job_cursor group by 1 order by 1""",
        "acquisitionState": """select entry_id,scope_kind,count(*) as subjects from ops.data_acquisition_state group by 1,2 order by 1,2""",
    }
    for field in ("order", "receivable", "rea"):
        join = "p.request_key=c.request_key" if field == "rea" else "p.attempt_id=c.response_id"
        queries[
            "enterprise_" + field
        ] = f"""select c.status,count(*) as rows,count(*) filter(where p.request_key is null) as missing_raw,
            count(*) filter(where c.request_key is distinct from p.request_key) as different_request from core.enterprise_{field}_supplement c
            left join raw.enterprise_{field}_response p on {join} group by 1 order by 1"""
    results = {k: rows(s, q) for k, q in queries.items()}
    quote_fields = "open high low close pre_close pct_chg".split()
    totals = Counter()
    for q in rows(
        s,
        """select q.*,p.payload->'fields' as fields,p.payload->'items' as items from core.market_daily_quote q
            join raw.source_payload p on p.id=q.source_payload_id order by q.id""",
    ):
        found = [dict(zip(q["fields"], it, strict=True)) for it in q["items"]]
        matches = [
            it
            for it in found
            if it.get("ts_code") == q["asset_code"]
            and str(it.get("trade_date")) == q["trade_date"].strftime("%Y%m%d")
        ]
        totals["matched"] += bool(matches)
        totals["missing"] += not matches
        for name in quote_fields:
            if matches:
                totals["numericFieldsCompared"] += 1
                totals["different"] += any(Decimal(str(it[name])) != q[name] for it in matches)
    results["marketQuoteRawValues"] = dict(totals)
    return results


def request_groups(s, rows, dividends):
    """非分红按请求逐组列出异文、额外捕获及当时已保存的前次凭证。"""
    captures = rows(
        s,
        """select p.id,b.source,p.api_name,p.request_hash,p.content_hash,p.captured_at,b.id as batch_id,
        b.dataset,b.status,b.started_at,b.finished_at,b.request_count,b.rejected_count
        from raw.source_payload p join raw.ingestion_batch b on b.id=p.batch_id order by p.captured_at,p.id""",
    )
    from foundation_audit_dividends import receipts

    batch_links, targets, _ = receipts(s)
    cached = {}
    for cursor, receipt in s.execute(
        text(
            "select cursor_key,cursor_value from ops.job_cursor where job_name='sync-stock-data' and cursor_value::jsonb ? 'batchId'"
        )
    ):
        for attempt in [*receipt.get("previousAttempts", []), receipt]:
            if attempt.get("cachedSourcePayloadId"):
                cached[attempt["batchId"]] = (attempt["cachedSourcePayloadId"], cursor)
    by_id = {r["id"]: r for r in captures}
    known = {r["id"]: r for r in dividends["captures"]}
    chains = defaultdict(list)
    for r in captures:
        chains[(r["source"], r["api_name"], r["request_hash"])].append(r)
    output = []
    for (source, api, h), chain in chains.items():
        if len(chain) < 2:
            continue
        summary = []
        seen = {}
        for i, r in enumerate(chain):
            old = seen.get(r["content_hash"])
            previous = chain[i - 1] if i else None
            decision = "首条捕获"
            basis = None
            pointer = None
            if i:
                decision = "证据不足"
                basis = "请求与正文相同，历史记录未证明可跨任务共享范围、时效及分页"
                if api == "dividend" and old:
                    detail = known[r["id"]]
                    decision, basis, pointer = (
                        detail["decision"],
                        detail["reason"],
                        detail.get("cachedSourcePayloadId"),
                    )
                elif previous["content_hash"] != r["content_hash"]:
                    decision, basis = "应重新获取", "同请求较前次有异文；保留该次修订或响应差异"
                elif r["batch_id"] in cached:
                    pid, _ = cached[r["batch_id"]]
                    p = by_id.get(pid, {})
                    if (
                        p.get("request_hash") == h
                        and p["content_hash"] == r["content_hash"]
                        and p["status"] == "completed"
                        and p["finished_at"]
                        and p["finished_at"] <= r["started_at"]
                        and r["request_count"] == 0
                    ):
                        decision, basis, pointer = (
                            "可复用",
                            "已完成零请求批次按游标明确复用早先的同请求原件",
                            pid,
                        )
                elif api == "disclosure_date":
                    prior = next(
                        (
                            x
                            for x in reversed(chain[:i])
                            if x["finished_at"]
                            and x["finished_at"] <= r["started_at"]
                            and x["status"] == "completed"
                        ),
                        None,
                    )
                    current_link = batch_links.get(r["batch_id"])
                    prior_link = batch_links.get(prior["batch_id"]) if prior else None
                    current_day = targets.get(current_link[1].split(":")[0]) if current_link else None
                    prior_day = targets.get(prior_link[1].split(":")[0]) if prior_link else None
                    if current_day and prior_day and current_day > prior_day:
                        decision, basis = (
                            "应重新获取",
                            f"公司行为复核目标日由 {prior_day} 推进至 {current_day}，原完成批次不能证明新目标日的披露计划",
                        )
            summary.append(
                {
                    "rawId": r["id"],
                    "dataset": r["dataset"],
                    "batch": r["batch_id"],
                    "status": r["status"],
                    "captureTime": r["captured_at"],
                    "batchStart": r["started_at"],
                    "contentHash": r["content_hash"],
                    "batchRequests": r["request_count"],
                    "previousEqual": old,
                    "decision": decision,
                    "basis": basis,
                    "referencedRawId": pointer,
                }
            )
            seen[r["content_hash"]] = r["id"]
        output.append(
            {
                "source": source,
                "api": api,
                "hash": h,
                "variants": len(seen),
                "extra": len(chain) - len(seen),
                "chain": summary,
            }
        )
    return output
