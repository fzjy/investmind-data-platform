from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from sqlalchemy import select

from app.connectors.tushare.contracts import TushareResponse, request_hash
from app.db.models import Security, SourcePayload, IngestionBatch, JobCursor
from app.ingestion.repositories import IngestionRepository, content_hash
from app.market.data.repository import aware
from app.market.pulse.repository import PulseRepository

JOB_NAME = 'sync-security-region'
FIELDS = ('ts_code', 'area')
LINEAGE = '_regionLineage'


def parameters(code: str) -> dict:
    return {'ts_code': code}


def region_value(value) -> str:
    if not isinstance(value, str):
        raise ValueError('REGION_RESPONSE_REJECTED')
    value = value.strip()
    if (not value or len(value) > 64 or any(ord(char) < 32 for char in value)
        or value.casefold() in {'--', '-', '未知', '未知地域', 'none', 'null', 'nan', 'n/a'}):
        raise ValueError('REGION_RESPONSE_REJECTED')
    return value


def normalize(code: str, response: TushareResponse) -> str:
    if len(response.fields) != 2 or set(response.fields) != set(FIELDS) or len(response.items) != 1 or len(response.items[0]) != 2:
        raise ValueError('REGION_RESPONSE_REJECTED')
    row = dict(zip(response.fields, response.items[0], strict=True))
    if row['ts_code'] != code:
        raise ValueError('REGION_RESPONSE_REJECTED')
    return region_value(row['area'])


def identity(code: str, fingerprint: str) -> str:
    return hashlib.sha256(f'region-v1:{fingerprint}:{code}'.encode()).hexdigest()


def _proof(session, security, checked_at: datetime, fingerprint=None):
    try:
        proof = security.data[LINEAGE]
        fp = proof['fingerprint']
        if not isinstance(fp, str) or not fp or (fingerprint is not None and fp != fingerprint):
            return None
        if type(proof['sourcePayloadId']) is not int or proof['sourcePayloadId'] <= 0:
            return None
        raw = session.get(SourcePayload, proof['sourcePayloadId'])
        if (raw is None or raw.api_name != 'stock_basic' or raw.content_hash != content_hash(raw.payload)
            or proof['contentHash'] != raw.content_hash or aware(raw.captured_at) > checked_at):
            return None
        batch = session.get(IngestionBatch, raw.batch_id)
        if (batch is None or batch.source != 'tushare' or batch.status != 'completed'
            or batch.dataset != 'security:region'):
            return None
        params = parameters(security.ts_code)
        if raw.payload['regionRequest'] != params or raw.request_hash != request_hash('stock_basic', params, FIELDS):
            return None
        label = normalize(security.ts_code, TushareResponse.model_validate(raw.payload))
        if region_value(security.data['area']) != label:
            return None
        # 普通writer保留area，遗留region若与本次供应商明确值冲突则不可混用。
        if security.data.get('region') not in (None, '') and region_value(security.data['region']) != label:
            return None
        cursor = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME,
            JobCursor.cursor_key == identity(security.ts_code, fp)))
        if cursor is None or cursor.cursor_value != {'sourcePayloadId':raw.id, 'contentHash':raw.content_hash}:
            return None
        return {'label':label, 'capturedAt':aware(raw.captured_at).isoformat()}
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def valid_regions(session, securities, checked_at: datetime, refresh_days: int = 30) -> dict:
    result = {}
    for security in securities:
        data = security.data or {}
        if LINEAGE in data:
            region = _proof(session, security, checked_at)
            if region is not None and checked_at < datetime.fromisoformat(region['capturedAt']) + timedelta(days=refresh_days):
                result[security.ts_code] = region
        else:
            try:
                labels = {region_value(data[key]) for key in ('area','region') if data.get(key) not in (None, '')}
                if len(labels) == 1:
                    result[security.ts_code] = {'label':labels.pop(), 'capturedAt':None}
            except ValueError:
                pass
    return result


class RegionRepository:
    def __init__(self, factory):
        self.factory = factory

    identity = staticmethod(identity)

    def target(self, selection, checked_at):
        return PulseRepository(self.factory).target(selection, checked_at)

    def is_complete(self, code, fingerprint, checked_at, refresh_days) -> bool:
        with self.factory() as session:
            security = session.get(Security, code)
            region = _proof(session, security, checked_at, fingerprint) if security else None
            return region is not None and checked_at < datetime.fromisoformat(region['capturedAt']) + timedelta(days=refresh_days)

    def persist(self, code, fingerprint, response, captured_at) -> bool:
        if captured_at.utcoffset() is None:
            raise ValueError('REGION_AWARE_CLOCK_REQUIRED')
        with self.factory.begin() as session:
            ingestion = IngestionRepository(session)
            batch = ingestion.start_batch('security:region', 'live')
            batch.started_at = batch.finished_at = captured_at
            batch.request_count = 1
            raw = ingestion.save_payload(batch, 'stock_basic', parameters(code), FIELDS, response)
            raw.payload = {**raw.payload, 'regionRequest':parameters(code)}
            raw.content_hash, raw.captured_at = content_hash(raw.payload), captured_at
            try:
                label = normalize(code, response)
            except ValueError:
                batch.status, batch.rejected_count = 'rejected', len(response.items)
                ingestion.reject(batch, 'REGION_RESPONSE_REJECTED')
                return False
            security = session.scalar(select(Security).filter_by(ts_code=code).with_for_update().execution_options(populate_existing=True))
            if security is None:
                batch.status = 'rejected'
                ingestion.reject(batch, 'REGION_SECURITY_MISSING')
                return False
            security.data = {**(security.data or {}), 'area':label, LINEAGE:{
                'sourcePayloadId':raw.id, 'contentHash':raw.content_hash, 'fingerprint':fingerprint}}
            batch.status, batch.row_count = 'completed', 1
            ingestion.upsert(JobCursor, {'job_name':JOB_NAME,'cursor_key':identity(code,fingerprint)}, {
                'cursor_value':{'sourcePayloadId':raw.id,'contentHash':raw.content_hash},'updated_at':captured_at})
            from app.acquisition.adapters.markets import record_market
            record_market(session,'region',code,parameters(code),captured_at,batch_id=batch.id)
            return True
