import importlib
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func, select

from app.connectors.tushare.contracts import TushareResponse
from app.db.models import DailyPrice, IngestionBatch, JobCursor, RejectedRecord, Security, SourcePayload

from backend.tests.market_data.test_normalization import FIELDS, target

NOW = datetime.fromisoformat('2026-10-15T15:13:00+00:00')


def module():
    assert importlib.util.find_spec('app.market.data.repository') is not None, 'market repository missing'
    return importlib.import_module('app.market.data.repository')


def setup(new_pg_factory):
    value = module()
    factory = new_pg_factory()
    return value, factory, value.MarketQuoteRepository(factory)


def response(close=100, *, day='20261015', code='000001.SH'):
    return TushareResponse(fields=FIELDS, items=((code, day, close, close, close, close, 100, close - 100),))


def test_valid_asset_commit_includes_raw_core_and_cursor(new_pg_factory):
    value, factory, repo = setup(new_pg_factory)
    assert repo.is_complete(target(), checked_at=NOW) is False
    result = repo.persist(target(), response(), captured_at=NOW)
    assert result['status'] == 'completed' and result['retainedCount'] == 1
    assert repo.is_complete(target(), checked_at=NOW) is True
    with factory() as session:
        quote = session.scalar(select(value.MarketDailyQuote))
        raw = session.get(SourcePayload, quote.source_payload_id)
        assert quote.asset_code == '000001.SH' and quote.asset_type == 'indices'
        assert raw.api_name == 'index_daily' and raw.payload['items'][0][0] == '000001.SH'
        assert session.scalar(select(func.count()).select_from(IngestionBatch)) == 1
        assert session.scalar(select(func.count()).select_from(JobCursor)) == 1
        assert session.scalar(select(func.count()).select_from(Security)) == 0
        assert session.scalar(select(func.count()).select_from(DailyPrice)) == 0


def test_identical_content_keeps_core_lineage_but_revision_preserves_old_raw(new_pg_factory):
    value, factory, repo = setup(new_pg_factory)
    repo.persist(target(), response(), captured_at=NOW)
    first = repo.latest(target(), checked_at=NOW)
    repo.persist(target(), response(), captured_at=NOW + timedelta(seconds=1))
    same = repo.latest(target(), checked_at=NOW + timedelta(seconds=1))
    assert (same.captured_at, same.source_payload_id) == (first.captured_at, first.source_payload_id)
    repo.persist(target(), response(101), captured_at=NOW + timedelta(seconds=2))
    revised = repo.latest(target(), checked_at=NOW + timedelta(seconds=2))
    assert revised.close == Decimal('101') and revised.source_payload_id != first.source_payload_id
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SourcePayload)) == 3
        assert session.get(SourcePayload, first.source_payload_id) is not None


def test_rejected_window_preserves_raw_but_not_success_cursor(new_pg_factory):
    value, factory, repo = setup(new_pg_factory)
    result = repo.persist(target(), response(code='000001.SZ'), captured_at=NOW)
    assert result['status'] == 'rejected'
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SourcePayload)) == 1
        assert session.scalar(select(func.count()).select_from(RejectedRecord)) == 1
        assert session.scalar(select(func.count()).select_from(JobCursor)) == 0
        assert session.scalar(select(func.count()).select_from(value.MarketDailyQuote)) == 0


@pytest.mark.parametrize('damage', ['missing_core', 'illegal_core', 'changed_core', 'raw_mismatch', 'raw_hash'])
def test_success_cursor_with_missing_or_corrupt_core_is_not_complete(new_pg_factory, damage):
    value, factory, repo = setup(new_pg_factory)
    repo.persist(target(), response(), captured_at=NOW)
    with factory.begin() as session:
        quote = session.scalar(select(value.MarketDailyQuote))
        raw = session.get(SourcePayload, quote.source_payload_id)
        if damage == 'missing_core':
            session.delete(quote)
        elif damage == 'illegal_core':
            quote.pct_chg = Decimal('7')
        elif damage == 'changed_core':
            quote.open = quote.high = quote.low = quote.close = Decimal('101')
            quote.pct_chg = Decimal('1')
        elif damage == 'raw_mismatch':
            raw.api_name = 'fund_daily'
        elif damage == 'raw_hash':
            raw.content_hash = '0' * 64
    assert repo.is_complete(target(), checked_at=NOW) is False


def test_complete_quote_requires_readable_raw_payload(new_pg_factory, monkeypatch):
    value, factory, repo = setup(new_pg_factory)
    repo.persist(target(), response(), captured_at=NOW)
    with factory.begin() as session:
        quote = session.scalar(select(value.MarketDailyQuote))
        raw_id = quote.source_payload_id
        with pytest.raises(IntegrityError), session.begin_nested():
            session.delete(session.get(SourcePayload, raw_id))
            session.flush()
    assert repo.is_complete(target(), checked_at=NOW)

    reads = []
    original_get = factory.class_.get

    def get(session, model, identity, *args, **kwargs):
        if model is SourcePayload and identity == raw_id:
            reads.append(identity)
            return None
        return original_get(session, model, identity, *args, **kwargs)

    monkeypatch.setattr(factory.class_, "get", get)
    assert repo.is_complete(target(), checked_at=NOW) is False
    assert reads == [raw_id]


def test_future_capture_is_not_visible_and_rejection_preserves_success(new_pg_factory):
    value, factory, repo = setup(new_pg_factory)
    repo.persist(target(), response(), captured_at=NOW)
    assert repo.is_complete(target(), checked_at=NOW - timedelta(seconds=1)) is False
    with factory() as session:
        original = session.scalar(select(JobCursor)).cursor_value
    assert repo.persist(target(), response(close=0), captured_at=NOW)['status'] == 'rejected'
    assert repo.is_complete(target(), checked_at=NOW) is True
    with factory() as session:
        assert session.scalar(select(JobCursor)).cursor_value == original


def test_missing_migration_is_safe_without_auto_create(new_pg_factory):
    value, factory, repo = setup(new_pg_factory)
    value.MarketDailyQuote.__table__.drop(factory.kw['bind'])
    with pytest.raises(ValueError, match='MARKET_MIGRATION_REQUIRED'):
        repo.is_complete(target(), checked_at=NOW)


def test_duplicate_identical_raw_rows_remain_replayable_complete_proof(new_pg_factory):
    _, _, repo = setup(new_pg_factory)
    original = response()
    duplicate = TushareResponse(fields=original.fields, items=original.items * 2)
    assert repo.persist(target(), duplicate, captured_at=NOW)['status'] == 'completed'
    assert repo.is_complete(target(), checked_at=NOW) is True
