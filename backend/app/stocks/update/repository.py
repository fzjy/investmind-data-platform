from dataclasses import asdict
from datetime import date, datetime, timezone
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError
from app.connectors.tushare.strategies import Dataset, RequestPlan
from app.connectors.tushare.contracts import TushareResponse, request_hash
from app.db.models import JobCursor, IngestionBatch, TradeCalendar, DisclosureSchedule, Dividend, SourcePayload
from app.ingestion.pipeline import IngestionPipeline
from app.ingestion.repositories import IngestionRepository
from app.stocks.update.planning import MARKET, FINANCIAL, CARD_FIELDS, financial_requirement_met
from app.ingestion.normalizers import normalize_financial_row
from app.stocks.update.validation import validate_response, strict_date

JOB_NAME = 'sync-stock-data'


class StockUpdateRepository:
    def __init__(self, factory):
        self.factory = factory

    def state(self, identity):
        with self.factory() as s:
            return {r.cursor_key[len(identity)+1:]: dict(r.cursor_value) for r in s.scalars(select(JobCursor).where(
                JobCursor.job_name == JOB_NAME, JobCursor.cursor_key.startswith(identity+':')))}

    @staticmethod
    def set_state(session, identity, unit, value):
        key = identity+':'+unit
        row = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME, JobCursor.cursor_key == key))
        if row is None:
            row = JobCursor(job_name=JOB_NAME, cursor_key=key)
            session.add(row)
        row.cursor_value, row.updated_at = value, datetime.now(timezone.utc)

    def save_manifest(self, identity, stage, plans):
        key = 'manifest:'+stage
        with self.factory.begin() as s:
            row = s.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME, JobCursor.cursor_key == identity+':'+key))
            if row is None:
                value = {'plans': [asdict(p) for p in plans]}
                self.set_state(s, identity, key, value)
            else:
                value = row.cursor_value
        return tuple(RequestPlan(dataset=Dataset(p['dataset']), api_name=p['api_name'], params=p['params'],
            fields=tuple(p['fields']), cursor_key=p['cursor_key'], page_size=p['page_size'],
            required_visible_since=p.get('required_visible_since')) for p in value['plans'])

    @staticmethod
    def requirement_satisfied(session, plan, as_of):
        if plan.dataset in FINANCIAL:
            model = FINANCIAL[plan.dataset]
            rows = session.scalars(select(model).where(model.ts_code == plan.params['ts_code'],
                model.end_date == strict_date(plan.params['period']))).all()
            since = date.fromisoformat(plan.required_visible_since) if plan.required_visible_since else date.min
            return financial_requirement_met(rows, as_of, since, plan.dataset, plan.fields)
        if plan.dataset in MARKET and strict_date(plan.params['end_date']) >= as_of:
            model = MARKET[plan.dataset]
            field = {Dataset.DAILY: 'close', Dataset.DAILY_BASIC: 'total_mv', Dataset.ADJ_FACTOR: 'adj_factor'}[plan.dataset]
            from app.shared.stock_mapping import finite_decimal
            row = session.scalar(select(model).where(model.ts_code == plan.params['ts_code'], model.trade_date == as_of))
            value = finite_decimal(getattr(row, field, None))
            return value is not None and value > 0
        return True

    def _missing(self, session, plan, response):
        retained = []
        for row in response.rows():
            dataset = plan.dataset
            if dataset in MARKET:
                model = MARKET[dataset]
                keys = {'ts_code': row['ts_code'], 'trade_date': strict_date(row['trade_date'])}
            elif dataset == Dataset.TRADE_CALENDAR:
                model, keys = TradeCalendar, {'exchange': row['exchange'], 'cal_date': strict_date(row['cal_date'])}
            elif dataset in FINANCIAL:
                model = FINANCIAL[dataset]
                keys = {'ts_code': row['ts_code'], 'end_date': strict_date(row['end_date']),
                    'ann_date': strict_date(row.get('ann_date')), 'report_type': row.get('report_type'), 'update_flag': row.get('update_flag')}
            elif dataset == Dataset.DIVIDEND:
                model, keys = Dividend, {'ts_code': row['ts_code'], 'end_date': strict_date(row['end_date']),
                    'ann_date': strict_date(row.get('ann_date')), 'div_proc': row.get('div_proc')}
            else:
                model, keys = DisclosureSchedule, {'ts_code': row['ts_code'], 'end_date': strict_date(row['end_date'])}
            existing = session.scalar(select(model).filter_by(**keys))
            if existing is not None:
                if dataset == Dataset.CASHFLOW:
                    disclosed = strict_date(row.get('f_ann_date')) or keys['ann_date']
                    if disclosed >= (existing.f_ann_date or existing.ann_date or date.min):
                        normalized = normalize_financial_row(row)['data']
                        old = existing.data or {}
                        if any(k not in old or old[k] != v for k, v in normalized.items()):
                            retained.append(tuple(row[f] for f in response.fields))
                    continue
                if dataset in CARD_FIELDS:
                    normalized = normalize_financial_row(row)['data']
                    old = existing.data or {}
                    if any(f not in old or old[f] != normalized[f] for f in CARD_FIELDS[dataset] & normalized.keys()):
                        retained.append(tuple(row[f] for f in response.fields))
                    continue
                if dataset == Dataset.TRADE_CALENDAR and existing.is_open != bool(row['is_open']):
                    raise ValueError('calendar conflicts with existing evidence')
                # Existing disclosure schedule is evidence; do not replace a later known announcement.
                if dataset == Dataset.DISCLOSURE_DATE:
                    ann, modified, actual = (strict_date(row.get(f)) for f in ('ann_date', 'modify_date', 'actual_date'))
                    old_revision = existing.modify_date or existing.ann_date or date.min
                    new_revision = modified or ann or date.min
                    if new_revision >= old_revision and (ann or date.min) >= (existing.ann_date or date.min):
                        # Never lose observed actual disclosure to a newer plan-only/null response.
                        if actual is None and existing.actual_date is not None:
                            row['actual_date'] = existing.actual_date.strftime('%Y%m%d')
                        if row.get('modify_date') is None and existing.modify_date is not None:
                            row['modify_date'] = existing.modify_date.strftime('%Y%m%d')
                        if any(strict_date(row.get(f)) != getattr(existing, f) for f in ('ann_date', 'pre_date', 'actual_date', 'modify_date')):
                            retained.append(tuple(row[f] for f in response.fields))
                continue
            retained.append(tuple(row[f] for f in response.fields))
        return TushareResponse(fields=response.fields, items=tuple(retained))

    def ingest_unit(self, identity, plan, response, as_of, window_start, *, request_count=1, cached_payload_id=None, attempts=None):
        from app.acquisition.adapters.stocks import stock_attempts, stock_result
        from app.ingestion.repositories import content_hash
        if attempts is None:
            attempts = stock_attempts(plan, identity)
        source = None
        with self.factory.begin() as session:
            if cached_payload_id is not None:
                source = session.get(SourcePayload, cached_payload_id)
                if (request_count != 0 or source is None or source.api_name != plan.api_name
                    or source.content_hash != content_hash(source.payload)
                    or source.request_hash != request_hash(plan.api_name, plan.params, plan.fields)
                    or TushareResponse.model_validate(source.payload) != response):
                    raise ValueError('cached evidence mismatch')
            prior = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME,
                JobCursor.cursor_key == identity+':'+plan.cursor_key))
            history = list(prior.cursor_value.get('previousAttempts', [])) if prior else []
            if prior:
                history.append({k: v for k, v in prior.cursor_value.items() if k != 'previousAttempts'})
            try:
                validated = validate_response(plan, response, as_of, window_start)
                missing = self._missing(session, plan, validated)
                # Any normalizer/constraint failure rolls all core rows back, then records raw+reject.
                with session.begin_nested():
                    result = IngestionPipeline(None, self.factory).persist_response(plan, missing, session=session,
                        mode='manual', raw_response=response, received_count=len(response.items))
                    if result.rejected_count:
                        raise ValueError('normalization rejected response')
                status, error, retained = 'completed', None, result.row_count
                batch = session.get(IngestionBatch, result.batch_id)
                if not self.requirement_satisfied(session, plan, as_of):
                    status, error = 'pending', 'required target/report evidence still missing'
            except (ValueError, KeyError, TypeError, DataError, IntegrityError) as exc:
                repo = IngestionRepository(session)
                batch = repo.start_batch(plan.dataset.value, 'manual')
                repo.save_payload(batch, plan.api_name, plan.params, plan.fields, response)
                error = str(exc) if isinstance(exc, ValueError) else 'invalid response'
                repo.reject(batch, error)
                status = 'incomplete' if len(response.items) >= plan.page_size else 'failed'
                batch.rejected_count, batch.row_count = 1, 0
                retained = 0
            batch.status, batch.request_count, batch.finished_at = status, request_count, datetime.now(timezone.utc)
            value = {'status': status, 'error': error, 'batchId': batch.id, 'received': len(response.items),
                'retained': retained, 'coverage': 'observed_response' if response.items else 'observed_missing',
                'previousAttempts': history}
            if plan.dataset == Dataset.DIVIDEND:
                value['rawOnlyNonImplemented'] = sum(row.get('div_proc') != '实施' for row in response.rows())
            if cached_payload_id is not None:
                value['cachedSourcePayloadId'] = cached_payload_id
            self.set_state(session, identity, plan.cursor_key, value)
            stock_result(session, attempts, value, batch.finished_at, cached=source)
            return value
