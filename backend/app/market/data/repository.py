from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, OperationalError

from app.connectors.tushare.contracts import TushareResponse, request_hash
from app.db.models import JobCursor, MarketDailyQuote, SourcePayload, IngestionBatch
from app.ingestion.repositories import IngestionRepository, content_hash
from app.market.data.contracts import MarketTarget, NormalizedMarketQuote, QUOTE_FIELDS
from app.market.data.normalization import normalize_market_response

JOB_NAME = 'sync-market-data'
PRICE_FIELDS = QUOTE_FIELDS[2:]


def aware(value: datetime) -> datetime:
    # SQLite omits timezone on read; production PG returns timezone-aware UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def quote_values(quote) -> dict[str, str]:
    return {key: str(Decimal(getattr(quote, key)).quantize(Decimal('0.00000001'))) for key in PRICE_FIELDS}


class MarketQuoteRepository:
    def __init__(self, factory) -> None:
        self.factory = factory

    def _rows(self, session, target):
        return session.scalars(select(MarketDailyQuote).where(
            MarketDailyQuote.provider == 'tushare', MarketDailyQuote.asset_type == target.asset.asset_type,
            MarketDailyQuote.asset_code == target.asset.asset_code,
            MarketDailyQuote.trade_date.between(target.start_date, target.end_date))
            .order_by(MarketDailyQuote.trade_date)).all()

    def _proof(self, rows):
        return [{'date': row.trade_date.isoformat(), 'values': quote_values(row),
            'sourcePayloadId': row.source_payload_id, 'capturedAt': aware(row.captured_at).isoformat()} for row in rows]

    def _lineage_valid(self, session, row, target, checked_at):
        if row.api_name != target.asset.api_name or aware(row.captured_at) > checked_at:
            return False
        raw = session.get(SourcePayload, row.source_payload_id)
        if raw is None or raw.api_name != target.asset.api_name or raw.content_hash != content_hash(raw.payload):
            return False
        batch = session.get(IngestionBatch, raw.batch_id)
        if batch is None or batch.source != 'tushare' or batch.status != 'completed' or aware(raw.captured_at) != aware(row.captured_at):
            return False
        response = TushareResponse.model_validate(raw.payload)
        params = raw.payload['marketRequest']
        if params.get('ts_code') != target.asset.asset_code or raw.request_hash != request_hash(raw.api_name, params, QUOTE_FIELDS):
            return False
        original = replace(target, start_date=datetime.strptime(params['start_date'],'%Y%m%d').date(),
            end_date=datetime.strptime(params['end_date'],'%Y%m%d').date())
        normalized = normalize_market_response(original, response, pct_tolerance=target.pct_tolerance)
        return any(q.trade_date == row.trade_date and quote_values(q) == quote_values(row) for q in normalized)

    def is_complete(self, target: MarketTarget, *, checked_at: datetime) -> bool:
        if checked_at.utcoffset() is None:
            raise ValueError('MARKET_AWARE_CLOCK_REQUIRED')
        try:
            with self.factory() as session:
                rows = self._rows(session, target)
                cursor = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME, JobCursor.cursor_key == target.identity))
                if not rows or cursor is None:
                    return False
                try:
                    return cursor.cursor_value.get('rows') == self._proof(rows) and all(
                        self._lineage_valid(session, row, target, checked_at) for row in rows)
                except (ValueError, TypeError, KeyError, ArithmeticError):
                    return False
        except (ProgrammingError, OperationalError) as exc:
            raise ValueError('MARKET_MIGRATION_REQUIRED') from exc

    def latest(self, target: MarketTarget, *, checked_at: datetime) -> MarketDailyQuote | None:
        if checked_at.utcoffset() is None:
            raise ValueError('MARKET_AWARE_CLOCK_REQUIRED')
        with self.factory() as session:
            for row in reversed(self._rows(session, target)):
                try:
                    if self._lineage_valid(session, row, target, checked_at):
                        return row
                except (ValueError, TypeError, KeyError, ArithmeticError):
                    continue
        return None

    def persist(self, target: MarketTarget, response: TushareResponse, *, captured_at: datetime,
        request_count: int = 1) -> dict:
        if captured_at.utcoffset() is None:
            raise ValueError('MARKET_AWARE_CLOCK_REQUIRED')
        with self.factory.begin() as session:
            ingestion = IngestionRepository(session)
            batch = ingestion.start_batch(f'market:{target.asset.asset_type}:{target.asset.asset_code}', 'live')
            batch.started_at = captured_at
            raw = ingestion.save_payload(batch, target.asset.api_name, target.params, QUOTE_FIELDS, response)
            # 安全请求参数保留原始窗口，不能用返回日期跨度冒充请求窗口。
            raw.payload = {**raw.payload, 'marketRequest': target.params}
            raw.content_hash = content_hash(raw.payload)
            raw.captured_at = captured_at
            batch.request_count = request_count
            batch.finished_at = captured_at
            try:
                quotes = normalize_market_response(target, response, pct_tolerance=target.pct_tolerance)
            except ValueError:
                batch.status = 'rejected'
                batch.rejected_count = len(response.items)
                ingestion.reject(batch, 'MARKET_RESPONSE_REJECTED')
                return {'status': 'rejected', 'retainedCount': 0, 'errorCode': 'MARKET_RESPONSE_REJECTED'}
            for quote in quotes:
                keys = dict(provider='tushare', asset_type=target.asset.asset_type,
                    asset_code=target.asset.asset_code, trade_date=quote.trade_date)
                existing = session.scalar(select(MarketDailyQuote).filter_by(**keys))
                values = {key: getattr(quote, key) for key in PRICE_FIELDS}
                # 修复断链/被篡改Raw时也必须建立可信的新血缘。
                same = existing is not None and existing.api_name == target.asset.api_name and quote_values(existing) == quote_values(quote)
                try:
                    same = same and self._lineage_valid(session, existing, target, captured_at)
                except (ValueError, TypeError, KeyError, ArithmeticError):
                    same = False
                if not same:
                    ingestion.upsert(MarketDailyQuote, keys, {**values, 'api_name': target.asset.api_name,
                        'captured_at': captured_at, 'source_payload_id': raw.id})
            batch.row_count = len(quotes)
            batch.status = 'completed'
            session.flush()
            rows = self._rows(session, target)
            ingestion.upsert(JobCursor, {'job_name': JOB_NAME, 'cursor_key': target.identity}, {
                'cursor_value': {'rows': self._proof(rows)}, 'updated_at': captured_at})
            from app.acquisition.adapters.markets import record_market
            record_market(session,target.asset.api_name,target.asset.asset_code,target.params,captured_at,batch_id=batch.id)
            return {'status': 'completed', 'retainedCount': len(quotes)}
