"""逐指标登记已发布和已留存的计算版本、输入引用与实际可回溯窗口。"""
import json
from pathlib import Path
import sys

from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.config import settings
from app.shared.readonly_database import open_readonly_factory


def main():
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        def rows(sql):
            return [dict(row) for row in session.execute(text(sql)).mappings()]

        metrics = rows("""
            with inputs as (
                select metric_value_id, count(*) as refs, count(content_hash) as hashed
                from derived.metric_input group by metric_value_id
            )
            select metric_id, formula_version, count(*) as rows, count(value) as usable,
                   count(distinct ts_code) as securities, count(distinct as_of) as dates,
                   min(as_of)::text as first_date, max(as_of)::text as last_date,
                   count(inputs.metric_value_id) as rows_with_refs,
                   coalesce(sum(refs),0)::bigint as refs, coalesce(sum(hashed),0)::bigint as hashed_refs
            from derived.metric_value v left join inputs on inputs.metric_value_id=v.id
            group by metric_id,formula_version order by metric_id, formula_version
        """)
        risk = rows("""
            select risk_id, rule_version, count(*) as rows, count(distinct ts_code) as securities,
                   min(as_of)::text as first_date, max(as_of)::text as last_date
            from derived.risk_result group by risk_id,rule_version order by risk_id,rule_version
        """)
        macro = rows("""
            select series_id, formula_version, count(*) as rows, count(value) as usable,
                   min(as_of)::text as first_date, max(as_of)::text as last_date
            from derived.macro_xray_result group by series_id,formula_version order by series_id,formula_version
        """)
    report = {"metricVersions": metrics, "riskRules": risk, "macroSeriesVersions": macro}
    dest = ROOT / "docs/research/investmind-derived-history.json"
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print({key: len(values) for key, values in report.items()})


if __name__ == "__main__":
    main()
