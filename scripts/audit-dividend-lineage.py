"""核对开发库中当前发布的三种股息率及其历史采集证据。数据库全程只读。"""

import json
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text
from app.config import settings
from app.shared.readonly_database import open_readonly_factory
from app.stocks.derived.workbench_cards import CARD_VERSION
from app.stocks.marts.screener_list import YIELD_CARDS


def query(session, sql, **params):
    return [dict(row) for row in session.execute(text(sql), params).mappings()]


def read_publication(session):
    version, as_of, published_at, stocks = session.execute(text("""
        select version, as_of, published_at, payload->'stocks' from mart.published_snapshot where product='screener'
    """)).one()
    if not stocks:
        raise ValueError("选股快照为空")
    symbols = sorted(stock["symbol"] for stock in stocks)
    if len(set(symbols)) != len(symbols):
        raise ValueError("证券代码重复")
    cards = query(session, """
        select ts_code, metric_id, formula_version, quality, value, missing_fields
        from derived.metric_value where as_of=:as_of and metric_id=any(:cards)
        and ts_code=any(:symbols) order by ts_code, metric_id, formula_version
    """, as_of=as_of.date(), cards=list(YIELD_CARDS.values()), symbols=symbols)
    card_index = {(r["ts_code"], r["metric_id"], r["formula_version"]): r for r in cards}
    versions = Counter(r["formula_version"] for r in cards)
    counts = Counter()
    evidence = {}
    mismatches = []
    for stock in sorted(stocks, key=lambda s: s["symbol"]):
        code = stock["symbol"]
        evidence[code] = {}
        for field, metric in YIELD_CARDS.items():
            value = stock[field]
            if field not in stock["researchValues"] or stock["researchValues"][field] != value:
                raise ValueError(f"{code} {field} 展示值与顶层字段不一致")
            card = card_index.get((code, metric, CARD_VERSION))
            result = {"value": value, "basis": stock["metricBasis"].get(field)}
            if value is None:
                counts[field] += 1
                result["publishedMissingReason"] = stock.get("listMissingReasons", {}).get(field)
            if not card:
                mismatches.append([code, field, "no stored card"])
            else:
                stored = float(card["value"]) if card["value"] is not None else None
                if value != stored:
                    mismatches.append([code, field, stored])
                if value is None:
                    result["cardMissingFields"] = card["missing_fields"]
            evidence[code][field] = result
    sources = query(session, """
        select v.metric_id, i.source_table, count(*) as input_records,
               count(distinct v.ts_code) as symbols
        from derived.metric_value v join derived.metric_input i on i.metric_value_id=v.id
        where v.as_of=:as_of and v.metric_id=any(:cards) and v.ts_code=any(:symbols)
        and v.formula_version=:version group by 1,2 order by 1,2
    """, as_of=as_of.date(), cards=list(YIELD_CARDS.values()), symbols=symbols, version=CARD_VERSION)
    return dict(version=version, asOf=as_of, publishedAt=published_at, symbols=len(symbols),
                inputRecordCounts=sources, codeCardVersion=CARD_VERSION,
                storedCardsByVersion=dict(versions), unmatchedCurrentCards=mismatches, nulls=dict(counts),
                bySymbol=evidence), symbols


def read_sources(session, symbols):
    sources = ["daily", "daily_basic", "dividend", "income", "fina_indicator"]
    params = {"apis": sources}
    captures = query(session, """
        select p.api_name, b.dataset, b.mode, count(*) as payloads,
               count(distinct (p.request_hash,p.content_hash)) as distinct_request_contents,
               min(p.captured_at) as captured_from, max(p.captured_at) as captured_through
        from raw.source_payload p join raw.ingestion_batch b on b.id=p.batch_id
        where p.api_name=any(:apis) group by 1,2,3 order by 1,2,3
    """, **params)
    batches = query(session, """
        select source, dataset, mode, status, count(*) as batches, sum(request_count) as requests,
               count(*) filter(where request_count=0) as zero_request_batches,
               min(started_at) as first_start, max(started_at) as last_start
        from raw.ingestion_batch b where b.dataset=any(:apis) or exists
        (select 1 from raw.source_payload p where p.batch_id=b.id and p.api_name=any(:apis))
        group by 1,2,3,4 order by 1,2,3,4
    """, **params)
    periods = {}
    for table, group in (("dividend", "ts_code,end_date"), ("income_statement", "ts_code,end_date,report_type"),
                         ("financial_indicator", "ts_code,end_date,report_type")):
        periods[table] = query(session, f"""
            with dates as (
                select {group}, min(ann_date) as first_ann, max(ann_date) as last_ann,
                       count(distinct ann_date) as announcements, count(*) as rows
                from core.{table} where ts_code=any(:symbols) group by {group}
            ) select count(*) as groups,sum(rows) as rows,min(end_date) as first_period,
                     max(end_date) as last_period,min(first_ann) as first_ann,max(last_ann) as last_ann,
                     count(*) filter(where announcements>1) as multi_announcement_groups
              from dates
        """, symbols=symbols)[0]
    return dict(capturedPayloads=captures, batchCounters=batches, observedCoreRanges=periods)


def main():
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        if session.scalar(text("show transaction_read_only")) != "on":
            raise ValueError("需要只读连接")
        publication, symbols = read_publication(session)
        report = dict(checkedAt=session.scalar(text("select current_timestamp")), publication=publication,
                      **read_sources(session, symbols))
        report["clockConfiguration"] = dict(timezone=settings.sync_timezone, hour=settings.sync_hour,
                                            minute=settings.sync_minute, routineStocks=settings.routine_stocks_enabled)
        report["stockUpdates"] = query(session, """
            select id, business_key, status, started_at, finished_at, request_count
            from ops.job_run where job_name='sync-stock-data' order by started_at,id
        """)
        report["schedulerHeartbeats"] = query(session, """
            select scheduler_name, state, count(*) as instances, max(last_heartbeat_at) as last_heartbeat
            from ops.scheduler_heartbeat group by 1,2 order by 1,2
        """)
        report["historicalScreenerVersions"] = query(session, """
            select as_of, count(*) as versions from mart.page_snapshot where product='screener'
            group by 1 order by 1
        """)
        print(json.dumps(report, default=str, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
