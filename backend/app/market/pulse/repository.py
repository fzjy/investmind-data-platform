from __future__ import annotations

import hashlib
from datetime import date, datetime

from sqlalchemy import select

from app.connectors.tushare.contracts import TushareResponse, request_hash
from app.db.models import DailyBasic, DailyPrice, PublishedSnapshot, SourcePayload, IngestionBatch, JobCursor
from app.ingestion.repositories import IngestionRepository, content_hash
from app.market.data.repository import aware

JOB_NAME = 'sync-market-pulse'
FIELDS = ('ts_code', 'trade_date', 'limit_status')
LINEAGE = '_limitStatusLineage'


def parameters(code: str, day: date) -> dict:
    return {'ts_code': code, 'trade_date': day.strftime('%Y%m%d')}


def status_value(value) -> int:
    if type(value) is int and 0 <= value <= 6:
        return value
    if type(value) is str and value in {'0', '1', '2', '3', '4', '5', '6'}:
        return int(value)
    raise ValueError('PULSE_RESPONSE_REJECTED')


def normalize(code: str, day: date, response: TushareResponse) -> int:
    if len(response.fields) != 3 or set(response.fields) != set(FIELDS) or len(response.items) != 1:
        raise ValueError('PULSE_RESPONSE_REJECTED')
    if len(response.items[0]) != 3:
        raise ValueError('PULSE_RESPONSE_REJECTED')
    row = dict(zip(response.fields, response.items[0], strict=True))
    if row['ts_code'] != code or row['trade_date'] != day.strftime('%Y%m%d'):
        raise ValueError('PULSE_RESPONSE_REJECTED')
    return status_value(row['limit_status'])


def identity(code: str, day: date, fingerprint: str) -> str:
    return hashlib.sha256(f'pulse-v1:{fingerprint}:{code}:{day}'.encode()).hexdigest()


def _valid(session, basic, checked_at: datetime, fingerprint: str | None = None) -> bool:
    try:
        proof = basic.data[LINEAGE]
        fp = proof['fingerprint']
        if fingerprint is not None and fp != fingerprint:
            return False
        raw = session.get(SourcePayload, proof['sourcePayloadId'])
        if raw is None or raw.api_name != 'daily_basic' or raw.content_hash != content_hash(raw.payload):
            return False
        if proof['contentHash'] != raw.content_hash or aware(raw.captured_at) > checked_at:
            return False
        batch = session.get(IngestionBatch, raw.batch_id)
        if (batch is None or batch.source != 'tushare' or batch.status != 'completed'
            or batch.dataset != 'market:pulse:limit_status'):
            return False
        params = parameters(basic.ts_code, basic.trade_date)
        if raw.payload['pulseRequest'] != params or raw.request_hash != request_hash('daily_basic', params, FIELDS):
            return False
        status = normalize(basic.ts_code, basic.trade_date, TushareResponse.model_validate(raw.payload))
        if status_value(basic.data['limit_status']) != status:
            return False
        cursor = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME,
            JobCursor.cursor_key == identity(basic.ts_code, basic.trade_date, fp)))
        return cursor is not None and cursor.cursor_value == {'sourcePayloadId': raw.id, 'contentHash': raw.content_hash}
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def validated_statuses(session, basics, checked_at: datetime) -> dict[str, int]:
    result = {}
    for basic in basics:
        if LINEAGE in (basic.data or {}):
            if _valid(session, basic, checked_at):
                result[basic.ts_code] = status_value(basic.data['limit_status'])
        else:
            try:
                result[basic.ts_code] = status_value((basic.data or {}).get('limit_status'))
            except ValueError:
                pass  # 兼容既有无标记字段；带标记损坏不得回退。
    return result


class PulseRepository:
    def __init__(self, factory):
        self.factory = factory

    identity = staticmethod(identity)

    def target(self, selection, checked_at: datetime) -> date:
        if checked_at.utcoffset() is None:
            raise ValueError('PULSE_AWARE_CLOCK_REQUIRED')
        with self.factory() as session:
            published = session.get(PublishedSnapshot, 'screener')
            if published is None:
                raise ValueError('PULSE_PUBLISHED_STOCKS_REQUIRED')
            day = published.as_of.date()
            if day > checked_at.date():
                raise ValueError('PULSE_FUTURE_TARGET')
            for model in (DailyBasic, DailyPrice):
                codes = set(session.scalars(select(model.ts_code).where(model.ts_code.in_(selection.symbols), model.trade_date == day)))
                if codes != set(selection.symbols):
                    raise ValueError('PULSE_STOCK_SCOPE_INCOMPLETE')
            return day

    def is_complete(self, code, day, fingerprint, checked_at) -> bool:
        with self.factory() as session:
            basic = session.scalar(select(DailyBasic).filter_by(ts_code=code, trade_date=day))
            return basic is not None and _valid(session, basic, checked_at, fingerprint)

    def persist(self, code, day, fingerprint, response, captured_at) -> bool:
        if captured_at.utcoffset() is None:
            raise ValueError('PULSE_AWARE_CLOCK_REQUIRED')
        with self.factory.begin() as session:
            ingestion = IngestionRepository(session)
            batch = ingestion.start_batch('market:pulse:limit_status', 'live')
            batch.started_at = batch.finished_at = captured_at
            batch.request_count = 1
            raw = ingestion.save_payload(batch, 'daily_basic', parameters(code, day), FIELDS, response)
            raw.payload = {**raw.payload, 'pulseRequest': parameters(code, day)}
            raw.content_hash = content_hash(raw.payload)
            raw.captured_at = captured_at
            try:
                status = normalize(code, day, response)
            except ValueError:
                batch.status, batch.rejected_count = 'rejected', len(response.items)
                ingestion.reject(batch, 'PULSE_RESPONSE_REJECTED')
                return False
            basic = session.scalar(select(DailyBasic).filter_by(ts_code=code, trade_date=day).with_for_update())
            if basic is None:
                batch.status = 'rejected'
                ingestion.reject(batch, 'PULSE_BASIC_MISSING')
                return False
            basic.data = {**basic.data, 'limit_status': status, LINEAGE: {
                'sourcePayloadId': raw.id, 'contentHash': raw.content_hash, 'fingerprint': fingerprint}}
            batch.status, batch.row_count = 'completed', 1
            ingestion.upsert(JobCursor, {'job_name': JOB_NAME, 'cursor_key': identity(code, day, fingerprint)}, {
                'cursor_value': {'sourcePayloadId': raw.id, 'contentHash': raw.content_hash}, 'updated_at': captured_at})
            from app.acquisition.adapters.markets import record_market
            record_market(session,'limit_status',code,parameters(code, day),captured_at,batch_id=batch.id)
            return True
