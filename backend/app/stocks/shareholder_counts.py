"""从已接受原件和 Core 重放股东户数，保留修订前观测及同日冲突。"""
from datetime import date
from types import SimpleNamespace

from sqlalchemy import String, cast, func, or_, select

from app.db.models import IngestionBatch, ShareholderCount, SourcePayload
from app.ingestion.repositories import content_hash


def load_shareholder_counts(session, symbols, as_of):
    codes = tuple(set(symbols))
    if not codes:
        return []
    rows = list(session.scalars(select(ShareholderCount).where(
        ShareholderCount.ts_code.in_(codes), ShareholderCount.end_date <= as_of,
        ShareholderCount.ann_date <= as_of)))
    # JSON 原件无请求参数列；先在数据库筛选证券，再按响应内容去重。
    # yield_per 限定读取内存；不依赖请求范围，覆盖日期范围回填原件。
    ids = select(func.min(SourcePayload.id)).join(IngestionBatch).where(
        SourcePayload.api_name == "stk_holdernumber",
        IngestionBatch.dataset == "shareholder_count", IngestionBatch.source == "tushare",
        IngestionBatch.status == "completed", IngestionBatch.rejected_count == 0,
        or_(*(cast(SourcePayload.payload, String).contains('"' + code + '"') for code in codes)),
    ).group_by(SourcePayload.request_hash, SourcePayload.content_hash)
    seen = set()
    for raw in session.scalars(select(SourcePayload).where(SourcePayload.id.in_(ids)).execution_options(yield_per=20)):
        payload = raw.payload
        fields = payload.get("fields", [])
        if raw.content_hash != content_hash(payload) or not {"ts_code", "ann_date", "end_date", "holder_num"} <= set(fields):
            continue
        for values in payload.get("items", []):
            item = dict(zip(fields, values))
            if item.get("ts_code") not in codes:
                continue
            try:
                ann = date.fromisoformat(str(item["ann_date"]).replace("-", ""))
                end = date.fromisoformat(str(item["end_date"]).replace("-", ""))
            except (ValueError, TypeError):
                continue
            key = (item["ts_code"], ann, end, str(item["holder_num"]))
            if ann > as_of or end > as_of or key in seen:
                continue
            seen.add(key)
            rows.append(SimpleNamespace(ts_code=item["ts_code"], ann_date=ann, end_date=end, holder_num=item["holder_num"]))
    return rows
