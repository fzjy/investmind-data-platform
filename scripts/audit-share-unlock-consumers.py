"""只读核对解禁来源的字段、业务日期、历史证据及发布并发校验的实际输入。"""
from collections import Counter
from datetime import date
from pathlib import Path
import json
import sys
from types import SimpleNamespace
from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings
from app.db.models import ShareUnlock, SourcePayload
from app.shared.readonly_database import open_readonly_factory
from app.stocks.update.publication import source_signature

FIELDS = ("float_share", "unlock_date", "float_date", "ann_date", "ts_code")


def audit(factory, session):
    assert session.scalar(text("show transaction_read_only")) == "on"
    now = session.scalar(text("select current_timestamp"))
    rows = list(session.scalars(select(ShareUnlock).order_by(ShareUnlock.id)))
    raw = list(session.scalars(select(SourcePayload).where(SourcePayload.api_name == "share_float").order_by(SourcePayload.id)))
    source = []
    raw_counts = {k:Counter() for k in FIELDS}
    for r in raw:
        body = r.payload
        fields = body.get("fields",[])
        items = [dict(zip(fields,row,strict=True)) for row in body["items"]]
        for k in FIELDS:
            raw_counts[k]["returnedPages"] += k in fields
            raw_counts[k]["withKey"] += sum(k in row for row in items)
            raw_counts[k]["nonNull"] += sum(row.get(k) is not None for row in items)
        source.append({"id":r.id,"capturedAt":r.captured_at.isoformat(),"requestHash":r.request_hash,
                       "returnedFields":fields,"rows":len(items),"contentHash":r.content_hash})
    core_counts = {}
    for k in FIELDS:
        matched=[r for r in rows if k in r.data]
        populated=[r for r in matched if r.data[k] is not None]
        dates=[r.ann_date.isoformat() for r in populated if r.ann_date]
        core_counts[k]={"withKey":len(matched),"withValue":len(populated),"earliestAnnounced":min(dates,default=None),
                        "latestAnnounced":max(dates,default=None),"sampleId":populated[0].id if populated else None}
    symbols, cutoff = session.execute(text("""
        select payload->'stocks', (as_of at time zone 'UTC')::date from mart.published_snapshot where product='screener'
    """)).one()
    current = {s["symbol"] for s in symbols}
    active = [r for r in rows if r.ts_code in current and r.ann_date and r.ann_date<=cutoff and r.float_date and r.float_date<=cutoff]
    # 使用未附着 SQLAlchemy 会话的内存对象，只替换一项 JSON 值，调用实际发布保护。
    template = rows[0] if rows else ShareUnlock(id=1,ts_code="600519.SH",ann_date=date(2025,1,1),float_date=date(2025,2,1),data={})
    snapshot = SimpleNamespace(**{k:() for k in ("securities","daily_prices","daily_basics","adjustment_factors",
        "financial_points","classifications","classification_members","trade_calendar")},security_inputs={})
    def signature(data):
        clone = ShareUnlock(id=template.id, ts_code=template.ts_code, ann_date=template.ann_date,
                            float_date=template.float_date,data=data)
        snapshot.corporate_actions = (clone,)
        return source_signature(snapshot)
    signed = {k:signature(dict(template.data) | {k:1}) != signature(dict(template.data) | {k:2}) for k in FIELDS[:2]}
    assert all(signed.values())
    daily=session.execute(text("""
        select count(*),count(*) filter(where data->>'float_share' is not null),min(trade_date),max(trade_date)
        from core.daily_basic
    """)).one()
    return {"checkedAt":now.isoformat(),"dataset":"share_float","priceCutoff":cutoff.isoformat(),"raw":source,
            "rawCount":len(raw),"rawRows":sum(s["rows"] for s in source),"rawFields":raw_counts,
            "coreRows":len(rows),"coreSymbols":len({r.ts_code for r in rows}),"coreFields":core_counts,
            "visibleInPool":len(active),"dynamicSignatureIncludesCandidate":signed,
            "dailyBasic":{"rows":daily[0],"floatSharePopulated":daily[1],"first":str(daily[2]),"last":str(daily[3])}}


if __name__ == "__main__":
    with open_readonly_factory(settings.database_read_url) as factory,factory() as session:
        print(json.dumps(audit(factory, session),indent=2,ensure_ascii=True))
