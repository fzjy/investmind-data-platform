"""统计同一请求/同一正文与不同历史修订的分布，全程只读。"""
import json
from pathlib import Path
import sys

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import settings
from app.shared.readonly_database import open_readonly_factory
from app.shared.errors import DataServiceError
from app.stocks.database_provider import StocksDatabaseProvider


class MissingPointer:
    def read(self, product):
        raise DataServiceError("DATA_NOT_READY", "模拟发布指针缺失")


def main():
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        def rows(query):
            return [dict(row) for row in session.execute(text(query)).mappings()]

        report = {
            "tables": rows("""
                select t.schemaname, t.tablename, pg_total_relation_size(u.relid) as bytes
                from pg_catalog.pg_tables t join pg_catalog.pg_stat_user_tables u
                on (u.schemaname,u.relname)=(t.schemaname,t.tablename)
                where t.schemaname in ('raw','core','ops','derived','mart')
                order by t.schemaname,t.tablename
            """),
            "raw": rows("""
                with by_content as (
                    select b.source, p.api_name, p.request_hash, p.content_hash, count(*) as n
                    from raw.source_payload p join raw.ingestion_batch b on b.id=p.batch_id
                    group by b.source, p.api_name, p.request_hash, p.content_hash
                ), by_request as (
                    select source, api_name, request_hash, sum(n) as n, count(*) as bodies
                    from by_content group by source, api_name, request_hash
                )
                select source, api_name, sum(n)::bigint as rows, count(*) as request_keys,
                       sum(n-bodies)::bigint as repeated_bodies,
                       count(*) filter(where bodies>1) as revised_request_keys
                from by_request group by source, api_name order by source, api_name
            """),
            "currentAndHistory": rows("""
                select split_part(p.product, ':', 1) as family, count(*) as current,
                       count(h.id) as also_in_history,
                       count(*) filter(where p.payload::jsonb=h.payload::jsonb) as identical
                from mart.published_snapshot p left join mart.page_snapshot h
                on h.product=p.product and h.snapshot_version=p.version group by 1 order by 1
            """),
            "screenerVersions": rows("""
                select as_of::text as date, count(*) as rows, count(distinct ts_code) as symbols,
                       count(distinct snapshot_version) as versions
                from mart.stock_screener_daily group by as_of order by as_of desc
            """),
        }
        fallback = StocksDatabaseProvider(MissingPointer(), factory).read_screener_universe()
        symbols = [row["symbol"] for row in fallback.raw["stocks"]]
        assert len(symbols) == len(set(symbols))
        report["screenerFallback"] = {"asOf": fallback.meta.model_dump(by_alias=True)["asOf"],
                                      "version": fallback.meta.version, "symbols": len(symbols)}
    output = ROOT / "docs/research/investmind-platform-storage.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(len(report["tables"]), "tables,", sum(row["repeated_bodies"] for row in report["raw"]), "repeat captures")


if __name__ == "__main__":
    main()
