"""只构建并原子提升选股器与固定股票工作台，保留现有质量门禁。"""
from dataclasses import replace, asdict
from datetime import date
from sqlalchemy import inspect, select
from app.db.models import PublishedSnapshot
from app.stocks.derived.inputs import CoreSnapshotRepository
from app.stocks.derived.pipeline import DerivedPipeline
from app.jobs.build_and_publish import BuildAndPublishJob, BuildAndPublishOptions
from app.jobs.build_history import SqlBuildHistory
from app.jobs.locks import data_publication_lock_key
from app.jobs.run_errors import JobLockedError
from app.marts.publication import SnapshotPublisher
from app.marts.quality import QualityGate
from app.stocks.marts.stocks import ScreenerMartBuilder, StockWorkbenchMartBuilder, load_derived_mart_rows, prepare_workbench_inputs
from app.ingestion.repositories import content_hash
from app.stocks.update.planning import StockUpdateOptions
from app.stocks.update.sync import StockUpdateSync, source_readiness


class StockSnapshotRepository:
    def __init__(self, factory):
        self.factory = factory
        self.snapshot = None

    def load(self, as_of, symbols):
        snapshot = CoreSnapshotRepository(self.factory).load(as_of, symbols)
        codes = set(symbols)
        updates = {field: tuple(row for row in getattr(snapshot, field) if row.ts_code in codes)
            for field in ('securities', 'daily_prices', 'daily_basics', 'adjustment_factors', 'corporate_actions',
                'classification_members', 'classification_history')}
        updates.update(security_inputs={k: v for k, v in snapshot.security_inputs.items() if k in codes},
            financial_points=tuple(p for p in snapshot.financial_points if p.ts_code in codes),
            macro_observations=(), content_documents=())
        self.snapshot = replace(snapshot, **updates)
        return self.snapshot


class StockMartBuilders:
    def __init__(self, factory):
        self.factory = factory

    def build(self, snapshot, symbols, version, *, workbench_symbols=None):
        codes = tuple(r.ts_code for r in snapshot.securities)
        if set(codes) != set(symbols) or set(workbench_symbols or symbols) != set(symbols):
            raise ValueError('stock candidate scope mismatch')
        rows = load_derived_mart_rows(self.factory, codes, snapshot.as_of)
        prepared = prepare_workbench_inputs(snapshot,self.factory)
        if self.factory is not None:
            with self.factory() as session:
                pointers = tuple(session.scalars(select(PublishedSnapshot.product).where(
                    PublishedSnapshot.product.startswith("stock-workbench:")).order_by(PublishedSnapshot.product)))
                if any(p.partition(":")[2] not in codes for p in pointers):
                    # 历史局部发布的导航可能已有缺项；合并已发布列表，再以本批行情刷新本批行。
                    published = {}
                    for product, old_rows in session.execute(select(PublishedSnapshot.product,
                            PublishedSnapshot.payload["sidebar"]["stocks"]).where(PublishedSnapshot.product.in_(pointers))
                            .order_by(PublishedSnapshot.product)):
                        for row in old_rows or ():
                            if row["symbol"] not in published or product == "stock-workbench:" + row["symbol"]:
                                published[row["symbol"]] = row
                    published.update({row["symbol"]: row for row in prepared.sidebar["stocks"]})
                    prepared = replace(prepared, sidebar=prepared.sidebar | {"stocks": list(published.values())})
        from app.stocks.marts.stocks import project_workbench_cards
        workbenches = tuple(StockWorkbenchMartBuilder(self.factory).build(snapshot, symbol, version,
            derived_rows=rows, prepared_inputs=prepared) for symbol in symbols)
        return (ScreenerMartBuilder(self.factory).build(snapshot, version, derived_rows=rows),
                *project_workbench_cards(self.factory, workbenches))


def source_signature(snapshot):
    def record(row):
        try:
            return {c.key: getattr(row, c.key) for c in inspect(type(row)).columns}
        except Exception:
            return asdict(row)
    data = {field: [record(row) for row in getattr(snapshot, field)] for field in (
        'securities', 'daily_prices', 'daily_basics', 'adjustment_factors', 'financial_points',
        'corporate_actions', 'classifications', 'classification_members', 'trade_calendar')}
    data['inputs'] = {k: asdict(v) for k, v in snapshot.security_inputs.items()}
    return content_hash(data)


def publish_stock_update(factory, selection, as_of, lock, *, collection, options=None):
    if collection.get('status') != 'completed' or not collection.get('publishReady'):
        raise ValueError('collection is not ready for publication')
    options = options or StockUpdateOptions(as_of=as_of)
    sync = StockUpdateSync(factory, None, selection, options, lock)
    planner = sync.planner
    identity = collection.get('identity')
    if not identity:
        raise ValueError('collection identity missing')
    repo = sync.repository

    def check():
        state = repo.state(identity)
        if state.get('target', {}).get('asOf') != as_of.isoformat():
            raise ValueError('collection target mismatch')
        for stage in ('calendar', 'actions', 'market', 'financial'):
            manifest = state.get('manifest:'+stage)
            if manifest is None or any(state.get(p['cursor_key'], {}).get('status') != 'completed' for p in manifest['plans']):
                raise ValueError('collection manifest incomplete')
            if stage == 'financial':
                from app.connectors.tushare.strategies import RequestPlan, Dataset
                with factory() as session:
                    for data in manifest['plans']:
                        plan = RequestPlan(Dataset(data['dataset']), data['api_name'], data['params'], tuple(data['fields']),
                            data['cursor_key'], data['page_size'], data.get('required_visible_since'))
                        if not repo.requirement_satisfied(session, plan, as_of):
                            raise ValueError('required financial inventory incomplete')
        upper = date.fromisoformat(collection['upperBound'])
        field_version = sync.contract_field_version(upper, identity)
        if planner.target(upper) != as_of or not source_readiness(factory, selection, as_of)['sourceReady']:
            raise ValueError('source readiness changed')
        if planner.stage_plans('financial', as_of, backfill_card_fields=field_version in (0, 3), legacy_cashflow=bool(field_version)):
            raise ValueError('new or remaining required financial inventory incomplete')

    source_key, publication_key = 'sync-stock-data:representative-50', data_publication_lock_key()
    if not lock.try_acquire(source_key):
        raise JobLockedError('stock source locked')
    try:
        if not lock.try_acquire(publication_key):
            raise JobLockedError('publication locked')
        try:
            check()
            repository = StockSnapshotRepository(factory)
            publisher = SnapshotPublisher(factory)
            class GuardedPublisher:
                def current(self, product):
                    return publisher.current(product)

                def publish_many(self, candidates, version, quality):
                    check()
                    current = StockSnapshotRepository(factory).load(as_of, selection.symbols)
                    if source_signature(current) != source_signature(repository.snapshot):
                        raise ValueError('stock source changed during build')
                    expected = {'screener', *(f'stock-workbench:{c}' for c in selection.symbols)}
                    if {c.product for c in candidates} != expected:
                        raise ValueError('stock publication scope mismatch')
                    previous = publisher.current("screener")
                    if previous and {r["symbol"] for r in previous.payload["stocks"]} - set(selection.symbols):
                        # 局部发布只提升工作台；共享选股器继续指向完整集合。
                        workbenches = tuple(c for c in candidates if c.product != "screener")
                        return publisher.publish_many(workbenches, version, quality,
                            expected_versions={"screener": previous.version})
                    return publisher.publish_many(candidates, version, quality)
            return BuildAndPublishJob(repository, DerivedPipeline(factory), StockMartBuilders(factory), QualityGate(),
                GuardedPublisher(), SqlBuildHistory(factory)).run(BuildAndPublishOptions(as_of, selection.symbols, selection.symbols))
        finally:
            lock.release(publication_key)
    finally:
        lock.release(source_key)
