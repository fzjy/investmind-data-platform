"""从原始风险输入重放现行规则，并逐证券对照接口、发布版本、持久化风险明细及历史策略。"""

from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import settings
from app.db.models import RiskResult
from app.shared.readonly_database import open_readonly_factory
from app.shared.repository import SnapshotRepository
from app.stocks.database_provider import StocksDatabaseProvider
from app.stocks.derived.risk_inputs import load_risk_inputs
from app.stocks.derived.workbench_risks import calculate_risks
from app.stocks.service import WorkbenchService


def main():
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        assert session.scalar(text("show transaction_read_only")) == "on"
        repo = SnapshotRepository(factory)
        service = WorkbenchService(StocksDatabaseProvider(repo, factory))
        products = session.execute(text("""
            select product, (as_of at time zone 'UTC')::date as day, version from mart.published_snapshot
            where product like 'stock-workbench:%' order by product
        """)).all()
        groups = defaultdict(dict)
        for product, day, version in products:
            groups[day][product.split(":", 1)[1]] = version
        assert groups
        totals = defaultdict(Counter)
        evidence = {}
        for day, versions in groups.items():
            sources = load_risk_inputs(session, list(versions), day)
            records = defaultdict(dict)
            for row in session.scalars(select(RiskResult).where(RiskResult.ts_code.in_(versions),
                                                                   RiskResult.as_of == day)):
                records[row.ts_code][row.risk_id] = row
            for symbol, version in versions.items():
                result = service.get_workbench(symbol)
                current = result.data["risks"]
                rebuilt = calculate_risks(sources[symbol])
                assert result.meta.version == version and current == rebuilt, symbol
                items = [item for group in current["groups"] for item in group["items"]]
                assert len(items) == len(records[symbol]) == 12
                statuses = Counter(item["status"] for item in items)
                score = max(0, 100 - 18*statuses["risk"] - 10*statuses["watch"] - 4*statuses["unknown"])
                assert current["score"] == (score if statuses["unknown"] <= 4 else None)
                assert current["assessment"]["knownCount"] == 12 - statuses["unknown"]
                for item in items:
                    key = item["id"]
                    record = records[symbol][key]
                    assert (record.snapshot_version, record.rule_version, record.inputs, record.status,
                            record.missing_fields, record.conclusion, record.formula) == (
                            version, current["ruleVersion"], item, item["status"], item["missingFields"],
                            item["conclusion"], item["formula"])
                    totals[key][item["status"]] += 1
                evidence[symbol] = {"version": version, "asOf": day.isoformat(), "ruleVersion": current["ruleVersion"],
                                    "score": current["score"], "level": current["level"],
                                    "status": {item["id"]: item["status"] for item in items}}
        history = session.execute(text("""
            select count(*) as versions, count(*) filter (where payload->'risks' is not null) as with_risks
            from mart.page_snapshot where product like 'stock-workbench:%'
        """)).mappings().one()
    print(json.dumps({"stocks": evidence, "coverageByRule": totals, "historicalPageRows": dict(history)},
                     ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
