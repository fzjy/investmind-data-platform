"""风险数据切换。每批的当前结果、指针和历史清理在同一事务内提交。"""
from sqlalchemy import delete, select, text
from sqlalchemy.orm import sessionmaker

from app.db.models import PageSnapshot, PublishedSnapshot, RiskResult, StockWorkbenchSnapshot
from app.jobs.locks import data_publication_lock_key, job_lock_for_engine
from app.marts.contracts import MartCandidate, MartRows, release_version
from app.marts.dto import validate_product_payload
from app.marts.publication import SnapshotPublisher
from app.stocks.marts.risk_cards import project_risks


def clean_history(session, symbol, execute):
    counts = []
    for model, key, value in ((PageSnapshot, "product", "stock-workbench:" + symbol),
                              (StockWorkbenchSnapshot, "ts_code", symbol)):
        if session.get_bind().dialect.name == "postgresql":
            table = model.__table__.fullname
            where = f" WHERE {key} = :value AND payload -> 'risks' IS NOT NULL"
            if execute:
                count = session.execute(text(f"UPDATE {table} SET payload = (payload::jsonb - 'risks')::json" + where),
                                        {"value": value}).rowcount
            else:
                count = session.scalar(text(f"SELECT count(*) FROM {table}" + where), {"value": value})
        else:
            rows = session.scalars(select(model).where(getattr(model, key) == value))
            count = 0
            for row in rows:
                if "risks" in row.payload:
                    count += 1
                    if execute:
                        row.payload = {k: v for k, v in row.payload.items() if k != "risks"}
        counts.append(count)
    return counts


def rebuild_workbench_risks(factory, *, execute=False):
    engine = factory.kw["bind"]
    lock, key = job_lock_for_engine(engine), data_publication_lock_key()
    if execute and not lock.try_acquire(key):
        return dict(status="locked")
    report = dict(status="completed" if execute else "planned", symbols=0, currentSets=0,
                  replacedRows=0, pageHistory=0, workbenchHistory=0)
    try:
        with factory() as session:
            published = select(PublishedSnapshot.product).where(PublishedSnapshot.product.startswith("stock-workbench:"))
            pages = select(PageSnapshot.product).where(PageSnapshot.product.startswith("stock-workbench:")).distinct()
            codes = {v.split(":", 1)[1] for query in (published, pages) for v in session.scalars(query)}
            codes.update(session.scalars(select(StockWorkbenchSnapshot.ts_code).distinct()))
            codes.update(session.scalars(select(RiskResult.ts_code).distinct()))
        ordered = sorted(codes)
        report["symbols"] = len(ordered)
        for offset in range(0, len(ordered), 5):
            batch = ordered[offset:offset + 5]
            with engine.begin() as connection:
                local = sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
                with local.begin() as session:
                    pointers = {code: session.get(PublishedSnapshot, "stock-workbench:" + code, with_for_update=execute)
                                for code in batch}
                    candidates = [MartCandidate(p.product, p.as_of, p.payload,
                        MartRows(charts=tuple(p.payload["dailyChart"]["candles"])),
                        stale=p.stale, stale_reason=p.stale_reason, workbench_parent_version=p.version)
                        for p in pointers.values() if p is not None]
                    changed = []
                    for candidate in project_risks(session, candidates):
                        symbol = candidate.payload["symbol"]
                        parent = pointers[symbol]
                        errors = validate_product_payload(candidate.product, candidate.payload)
                        if errors:
                            raise ValueError(f"{symbol} 工作台校验失败 {errors}")
                        stored = list(session.scalars(select(RiskResult).where(RiskResult.ts_code == symbol)))
                        risks = candidate.payload["risks"]
                        expected = {i["id"]: i for g in risks["groups"] for i in g["items"]}
                        if (risks == parent.payload.get("risks") and len(stored) == 12
                                and all(row.snapshot_version == parent.version and row.rule_version == risks["ruleVersion"]
                                        and row.inputs == expected.get(row.risk_id) for row in stored)):
                            continue
                        report["currentSets"] += 1
                        report["replacedRows"] += len(stored)
                        changed.append(candidate)
                    if execute and changed:
                        version = release_version(max(c.as_of for c in changed), changed)
                        SnapshotPublisher(local).publish_many(changed, version, (),
                            expected_versions={c.product: c.workbench_parent_version for c in changed},
                            preserve_freshness_check=True)
                    for symbol, parent in pointers.items():
                        if parent is None:
                            rows = list(session.scalars(select(RiskResult.id).where(RiskResult.ts_code == symbol)))
                            report["replacedRows"] += len(rows)
                            if rows and execute:
                                session.execute(delete(RiskResult).where(RiskResult.ts_code == symbol))
                        pages, snapshots = clean_history(session, symbol, execute)
                        report["pageHistory"] += pages
                        report["workbenchHistory"] += snapshots
        return report
    finally:
        if execute:
            lock.release(key)
