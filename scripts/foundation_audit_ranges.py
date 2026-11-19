"""按实际返回的业务键统计不同上游查询的范围交集。"""

from collections import Counter, defaultdict
from decimal import Decimal
import json
from sqlalchemy import text
from foundation_audit_dividends import business_rows

# 同一 API 内比对。左、右是键列与核对值，跳过缺失的关键维度。
SLICES = {
    "daily": ("ts_code trade_date", "close"),
    "adj_factor": ("ts_code trade_date", "adj_factor"),
    "daily_basic": ("ts_code trade_date", "pe pb limit_status"),
    "fund_daily": ("ts_code trade_date", "close pre_close"),
    "index_daily": ("ts_code trade_date", "close pre_close"),
    "index_global": ("ts_code trade_date", "close pre_close"),
    "fund_share": ("ts_code trade_date", "fd_share"),
    "fut_daily": ("ts_code trade_date", "close settle"),
    "fut_mapping": ("ts_code trade_date", "mapping_ts_code"),
    "dividend": (
        "ts_code end_date ann_date div_proc",
        "cash_div cash_div_tax imp_ann_date base_date base_share",
    ),
    "stock_basic": ("ts_code", "name area list_date"),
    "trade_cal": ("exchange cal_date", "is_open pretrade_date"),
    "disclosure_date": ("ts_code end_date", "actual_date pre_date"),
    "repurchase": ("ts_code ann_date proc", "amount"),
    "share_float": ("ts_code ann_date float_date", "float_share"),
    "stk_holdernumber": ("ts_code end_date", "holder_num"),
    "top10_holders": ("ts_code end_date holder_name", "hold_amount"),
    "income": ("ts_code end_date ann_date report_type", "n_income_attr_p"),
    "balancesheet": ("ts_code end_date ann_date report_type", "total_assets"),
    "cashflow": ("ts_code end_date ann_date report_type", "n_cashflow_act"),
}


def summarize(s):
    report = {}
    for api, (keys, checks) in SLICES.items():
        keys, checks = keys.split(), checks.split()
        samples, stats, skip = {}, Counter(), Counter()
        dataset_keys = defaultdict(set)
        for raw_id, req, group, head, body, transport in s.execute(
            text(
                """
            select p.id,p.request_hash,b.dataset,p.payload->'fields', (p.payload->'items')::text,
                   case when p.api_name='dividend' then p.payload->'_http' end
            from raw.source_payload p join raw.ingestion_batch b on b.id=p.batch_id
            where b.source='tushare' and p.api_name=:api order by p.captured_at,p.id"""
            ),
            {"api": api},
        ):
            if not head or body is None:
                skip["nonTabularPages"] += 1
                continue
            if api == "dividend" and (transport is not None or group == "portfolio_dividend_evidence"):
                payload = {"fields": head, "items": json.loads(body)}
                if transport is not None:
                    payload["_http"] = transport
                verified, _ = business_rows(raw_id, group, payload, None)
                values = [[row[field] for field in head] for row in verified]
            else:
                values = json.loads(body, parse_float=Decimal)
            if not set(keys) <= set(head):
                skip["pagesMissingKeyColumns"] += 1
                skip["rowsMissingKeyColumns"] += len(values)
                continue
            pos = [head.index(k) for k in keys]
            cols = {f: head.index(f) for f in checks if f in head}
            stats["pages"] += 1
            stats["rows"] += len(values)
            for row in values:
                k = tuple(row[i] for i in pos)
                if any(v in (None, "") for v in k):
                    skip["rowsMissingKeyValues"] += 1
                    continue
                dataset_keys[group].add(k)
                if k not in samples:
                    samples[k] = [req, False, {}, set(), raw_id]
                first, many, observed, conflicts, _ = samples[k]
                samples[k][1] = many or req != first
                for f, i in cols.items():
                    val = row[i]
                    if val is None or val == "":
                        continue
                    if f not in observed:
                        observed[f] = val
                    elif val != observed[f]:
                        conflicts.add(f)
        overlap = Counter()
        examples = []
        for k, (req, many, seen, conflicts, first) in samples.items():
            if many:
                overlap.update(conflicts)
                if conflicts and len(examples) < 3:
                    examples.append({"key": k, "fields": sorted(conflicts), "firstRawId": first})
        pairs = []
        for i, (left, left_keys) in enumerate(sorted(dataset_keys.items())):
            for right, right_keys in sorted(dataset_keys.items())[i + 1 :]:
                both = len(left_keys & right_keys)
                if both:
                    pairs.append(
                        {
                            "left": left,
                            "right": right,
                            "both": both,
                            "leftOnly": len(left_keys) - both,
                            "rightOnly": len(right_keys) - both,
                        }
                    )
        report[api] = {
            "keys": keys,
            "compareNonNullFields": checks,
            "stats": stats,
            "excluded": skip,
            "observedKeys": len(samples),
            "keysInMultipleRequests": sum(row[1] for row in samples.values()),
            "overlapKeysWithDifferentValues": dict(overlap),
            "examples": examples,
            "overlappingDatasetPairs": pairs,
        }
    return report
