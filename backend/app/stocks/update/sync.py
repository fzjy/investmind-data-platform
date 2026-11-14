"""固定池、冻结请求单元、共享物理预算；采集不会自动发布。"""
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from uuid import uuid4
from sqlalchemy import select
from app.stocks.classification.sync import RequestBudget, BudgetExhausted
from app.connectors.tushare.errors import TushareError
from app.db.models import JobRun, DailyPrice, DailyBasic, AdjustmentFactor
from app.stocks.derived.inputs import CoreSnapshotRepository
from app.ingestion.repositories import content_hash
from app.shared.stock_mapping import classification_fields, finite_decimal
from app.stocks.update.planning import StockUpdatePlanner, StockUpdateOptions, plan_for, MARKET, FINANCIAL
from app.connectors.tushare.strategies import Dataset, LEGACY_CASHFLOW_FIELDS
from app.stocks.update.repository import StockUpdateRepository, JOB_NAME


def source_readiness(factory, selection, as_of: date) -> dict:
    stocks = {c: {'market': {}, 'classification': 'not_assessed'} for c in selection.symbols}
    with factory() as s:
        for model, field in ((DailyPrice, 'close'), (DailyBasic, 'total_mv'), (AdjustmentFactor, 'adj_factor')):
            values = {r.ts_code: r for r in s.scalars(select(model).where(model.ts_code.in_(selection.symbols), model.trade_date == as_of))}
            for symbol in selection.symbols:
                value = finite_decimal(getattr(values.get(symbol), field, None))
                stocks[symbol]['market'][model.__tablename__] = 'observed' if value is not None and value > 0 else 'missing'
    snapshot = CoreSnapshotRepository(factory).load(as_of, selection.symbols, market_history=False)
    for security in snapshot.securities:
        if security.ts_code in stocks:
            _, issues = classification_fields(snapshot, security)
            stocks[security.ts_code]['classification'] = 'observed_classified' if not issues else 'missing'
    ready = all(all(v == 'observed' for v in info['market'].values())
        and info['classification'] == 'observed_classified' for info in stocks.values())
    return {'stocks': stocks, 'sourceReady': ready,
        'targetMarketComplete': sum(all(v == 'observed' for v in x['market'].values()) for x in stocks.values()),
        'classified': sum(x['classification'] == 'observed_classified' for x in stocks.values())}


class StockUpdateSync:
    def __init__(self, factory, client, selection, options: StockUpdateOptions, lock):
        self.factory, self.client, self.selection, self.options, self.lock = factory, client, selection, options, lock
        self.planner = StockUpdatePlanner(factory, selection, options)
        self.repository = StockUpdateRepository(factory)

    def preflight(self, now: datetime) -> dict:
        result = self.planner.preflight(now)
        active = self.repository.state('control').get('active', {})
        if active and active.get('status') != 'completed':
            if self.options.as_of and self.options.as_of.isoformat() != active['upperBound']:
                raise ValueError('unfinished target differs; resume original target first')
            result.update(upperBound=active['upperBound'], resumeIdentity=active['identity'])
            state = self.repository.state(active['identity'])
            frozen = state.get('target', {}).get('asOf')
            result.update(asOf=frozen, calendarPending=frozen is None)
        return result

    def _identity(self, upper, *, legacy_fields=0):
        datasets = (Dataset.TRADE_CALENDAR, Dataset.DIVIDEND, Dataset.DISCLOSURE_DATE, *MARKET, *FINANCIAL)
        contracts = [plan_for(dataset, {}) for dataset in datasets]
        if legacy_fields:
            contracts = [self._legacy_fields(plan, legacy_fields) for plan in contracts]
        contract = {'rules': 'stock-update-v1', 'pool': self.selection.to_payload(), 'upper': upper.isoformat(),
            'windowStart': self.options.start(upper).isoformat(), 'lookahead': self.options.calendar_lookahead_days,
            'contracts': [asdict(plan) for plan in contracts]}
        return content_hash(contract)

    def contract_field_version(self, upper, identity):
        for version in (0, 3, 1, 2):
            if identity == self._identity(upper, legacy_fields=version):
                return version
        raise ValueError('unfinished stock contract changed; restore original window and fields')

    @staticmethod
    def _legacy_fields(plan, version):
        if plan.dataset == Dataset.CASHFLOW:
            return replace(plan, fields=LEGACY_CASHFLOW_FIELDS)
        removed = set()
        if version in (1, 2):
            removed = {Dataset.INCOME: {'basic_eps'}, Dataset.FINANCIAL_INDICATOR: {'eps', 'bps'}}.get(plan.dataset, set())
        if version == 2 and plan.dataset == Dataset.FINANCIAL_INDICATOR:
            removed = removed | {'q_eps', 'q_roe'}
        return replace(plan, fields=tuple(f for f in plan.fields if f not in removed))

    def run(self, now: datetime) -> dict:
        preflight = self.preflight(now)
        result = {**preflight, 'status': 'running', 'requests': 0, 'publishReady': False}
        key = 'sync-stock-data:representative-50'
        if not self.lock.try_acquire(key):
            return {**result, 'status': 'locked'}
        budget = RequestBudget(self.options.max_requests)
        run_id = str(uuid4())
        try:
            preflight = self.preflight(now)
            upper = date.fromisoformat(preflight['upperBound'])
            identity = preflight.get('resumeIdentity') or self._identity(upper)
            legacy_fields = self.contract_field_version(upper, identity)
            result.update(identity=identity, upperBound=upper.isoformat())
            with self.factory.begin() as s:
                self.repository.set_state(s, 'control', 'active', {'identity': identity, 'upperBound': upper.isoformat(), 'status': 'running'})
                s.add(JobRun(id=run_id, job_name=JOB_NAME, business_key=identity, details=result))
            if self.client:
                self.client.set_before_request(budget.consume)
            for stage in ('calendar', 'actions', 'market', 'financial'):
                state = self.repository.state(identity)
                if stage == 'calendar':
                    target = upper
                else:
                    target = date.fromisoformat(state['target']['asOf'])
                existing = state.get('manifest:'+stage)
                # Do not recompute changed inventory on resume.
                plans = () if existing else (self.planner.calendar_plans(upper) if stage == 'calendar'
                    else self.planner.stage_plans(stage, target, backfill_card_fields=legacy_fields in (0, 3), legacy_cashflow=bool(legacy_fields)))
                if legacy_fields:
                    plans = tuple(self._legacy_fields(plan, legacy_fields) for plan in plans)
                manifest = self.repository.save_manifest(identity, stage, plans)
                for plan in manifest:
                    result.update(stage=stage, nextUnit=plan.cursor_key)
                    evidence = state.get(plan.cursor_key)
                    if evidence is None or evidence['status'] in {'failed', 'pending'}:
                        if self.client is None:
                            raise ValueError('client required')
                        before = budget.used
                        response = self.client.call(plan.api_name, plan.params, plan.fields)
                        evidence = self.repository.ingest_unit(identity, plan, response, target,
                            self.options.start(target), request_count=budget.used-before)
                    if evidence['status'] != 'completed':
                        result.update(status='partial' if evidence['status'] == 'pending' else evidence['status'], error=evidence['error'])
                        return result
                if stage == 'calendar':
                    target = self.planner.target(upper)
                    prior = self.repository.state(identity).get('target', {})
                    if prior and prior['asOf'] != target.isoformat():
                        raise ValueError('frozen target calendar changed')
                    with self.factory.begin() as s:
                        self.repository.set_state(s, identity, 'target', {'asOf': target.isoformat()})
                    result.update(asOf=target.isoformat(), calendarPending=False)
            result.update(status='completed', nextUnit=None)
            return result
        except BudgetExhausted:
            result.update(status='partial', error='physical HTTP budget exhausted')
            return result
        except (TushareError, ValueError) as exc:
            result.update(status='failed', error=str(exc))
            return result
        finally:
            try:
                result['requests'] = budget.used
                identity = result.get('identity')
                if identity:
                    state = self.repository.state(identity)
                    target = state.get('target', {}).get('asOf')
                    result['units'] = {k: v for k, v in state.items() if not k.startswith('manifest:') and k != 'target'}
                    if target:
                        as_of = date.fromisoformat(target)
                        result.update(asOf=target, **source_readiness(self.factory, self.selection, as_of))
                        calendar_ok = self.planner.target(date.fromisoformat(result['upperBound'])) == as_of
                        result['publishReady'] = result['status'] == 'completed' and calendar_ok and result['sourceReady']
                        result['historyGaps'] = [{'dataset': p.dataset.value, 'symbol': p.params['ts_code'],
                            'from': p.params['start_date'], 'to': p.params['end_date']}
                            for p in self.planner.stage_plans('market', as_of)]
                    else:
                        result['stocks'] = {c: {'coverage': 'not_assessed'} for c in self.selection.symbols}
                    with self.factory.begin() as s:
                        self.repository.set_state(s, 'control', 'active', {'identity': identity,
                            'upperBound': result['upperBound'], 'status': result['status']})
                        row = s.get(JobRun, run_id)
                        if row is not None:
                            row.status = result['status'] if result['status'] != 'running' else 'failed'
                            row.request_count, row.details, row.finished_at = budget.used, result, datetime.now(timezone.utc)
            finally:
                self.lock.release(key)
