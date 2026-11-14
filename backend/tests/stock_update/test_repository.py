from datetime import date

import pytest
from sqlalchemy import event, func, select

from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.strategies import Dataset
from app.db.models import DailyPrice, SourcePayload, JobCursor, RejectedRecord
from app.stocks.update.planning import plan_for
from backend.tests.derived.test_inputs import _factory


def sample(symbol='600519.SH', close=20):
    p = plan_for(Dataset.DAILY, {'ts_code': '600519.SH', 'start_date': '20261012', 'end_date': '20261013'})
    row = {'ts_code': symbol, 'trade_date': '20261013', 'close': close}
    return p, TushareResponse(fields=p.fields, items=(tuple(row.get(f) for f in p.fields),))


def test_commit_failure_rolls_back_raw_core_progress():
    from app.stocks.update.repository import StockUpdateRepository
    f = _factory()
    def fail(session):
        raise RuntimeError('commit unavailable')
    event.listen(f, 'before_commit', fail)
    p, r = sample()
    try:
        with pytest.raises(RuntimeError):
            StockUpdateRepository(f).ingest_unit('test', p, r, date(2026, 10, 13), date(2023, 10, 13))
    finally:
        event.remove(f, 'before_commit', fail)
    with f() as s:
        for model in (DailyPrice, SourcePayload, JobCursor):
            assert s.scalar(select(func.count()).select_from(model)) == 0


def test_existing_market_not_overwritten_raw_retained():
    from app.stocks.update.repository import StockUpdateRepository
    f = _factory()
    with f.begin() as s:
        s.add(DailyPrice(ts_code='600519.SH', trade_date=date(2026, 10, 13), close=10))
    p, r = sample()
    repo = StockUpdateRepository(f)
    result = repo.ingest_unit('test', p, r, date(2026, 10, 13), date(2023, 10, 13))
    assert result['status'] == 'completed' and result['retained'] == 0
    with f() as s:
        assert s.scalar(select(DailyPrice)).close == 10
        assert s.scalar(select(SourcePayload)).payload['items'][0][p.fields.index('close')] == 20
    assert repo.state('test')[p.cursor_key]['status'] == 'completed'


def test_invalid_response_retains_rejection_without_core():
    from app.stocks.update.repository import StockUpdateRepository
    f = _factory()
    p, r = sample(symbol='000001.SZ')
    result = StockUpdateRepository(f).ingest_unit('test', p, r, date(2026, 10, 13), date(2023, 10, 13))
    assert result['status'] == 'failed'
    with f() as s:
        assert s.scalar(select(func.count()).select_from(DailyPrice)) == 0
        assert s.scalar(select(func.count()).select_from(SourcePayload)) == 1
        assert s.scalar(select(func.count()).select_from(RejectedRecord)) == 1


def test_existing_financial_version_enriched_and_replay_is_idempotent():
    from app.db.models import FinancialIndicator
    from app.stocks.update.repository import StockUpdateRepository
    factory = _factory()
    period, day = date(2026, 9, 30), date(2026, 10, 13)
    with factory.begin() as session:
        session.add(FinancialIndicator(ts_code='600519.SH', end_date=period, ann_date=day, data={'legacy': 10}))
    plan = plan_for(Dataset.FINANCIAL_INDICATOR, {'ts_code': '600519.SH', 'period': '20260930'})
    row = dict(ts_code='600519.SH', end_date='20260930', ann_date='20261013', eps=3, bps=11, q_eps=None)
    response = TushareResponse(fields=plan.fields, items=(tuple(row.get(f) for f in plan.fields),))
    repo = StockUpdateRepository(factory)
    for expected in (1, 0):
        unit = repo.ingest_unit('fields', plan, response, day, date(2023, 1, 1))
        assert unit['status'] == 'completed' and unit['retained'] == expected
    with factory() as session:
        data = session.scalars(select(FinancialIndicator)).one().data
        assert {k: data[k] for k in ('eps', 'bps', 'q_eps', 'legacy')} == dict(eps=3, bps=11, q_eps=None, legacy=10)


def test_latest_parent_or_older_complete_versions_cannot_finish_card_backfill():
    from dataclasses import replace
    from app.db.models import IncomeStatement
    from app.stocks.update.repository import StockUpdateRepository
    factory = _factory()
    period, day = date(2026, 9, 30), date(2026, 10, 13)
    plan = plan_for(Dataset.INCOME, {'ts_code': '600519.SH', 'period': '20260930'})
    with factory.begin() as s:
        for typ, stamp, data in [('1', 3, {'basic_eps': 2}), ('4', 6, {}), ('2', 7, {}), ('6', 10, {'basic_eps': 5})]:
            s.add(IncomeStatement(ts_code='600519.SH', end_date=period, report_type=typ,
                ann_date=date(2026, 10, stamp), data=data))
    repo = StockUpdateRepository(factory)
    empty = TushareResponse(fields=plan.fields, items=())
    assert repo.ingest_unit('test', plan, empty, day, period)['status'] == 'pending'
    old = replace(plan, fields=tuple(k for k in plan.fields if k != 'basic_eps'))
    assert repo.ingest_unit('old', old, TushareResponse(fields=old.fields, items=()), day, period)['status'] == 'completed'
    for typ, stamp, value, expected in [('4', 6, 2, 'pending'), ('2', 7, None, 'completed'), ('4', 6, None, 'completed')]:
        raw = dict(ts_code='600519.SH', end_date='20260930', ann_date=f'202610{stamp:02d}', report_type=typ, basic_eps=value)
        response = TushareResponse(fields=plan.fields, items=(tuple(raw.get(f) for f in plan.fields),))
        assert repo.ingest_unit('test', plan, response, day, period)['status'] == expected
    with factory() as s:
        assert s.scalar(select(IncomeStatement).where(IncomeStatement.report_type == '4')).data['basic_eps'] is None


def test_manifest_does_not_shrink_on_resume():
    from app.stocks.update.repository import StockUpdateRepository
    repo = StockUpdateRepository(_factory())
    p, _ = sample()
    assert repo.save_manifest('test', 'market', (p,)) == (p,)
    assert repo.save_manifest('test', 'market', ()) == (p,)


def test_cached_response_requires_same_payload_and_request_evidence():
    from app.stocks.update.repository import StockUpdateRepository
    f = _factory()
    repo = StockUpdateRepository(f)
    p, r = sample()
    original = repo.ingest_unit('test', p, r, date(2026, 10, 13), date(2023, 10, 13))
    with f() as s:
        pid = s.scalar(select(SourcePayload).where(SourcePayload.batch_id == original['batchId'])).id
    cached = repo.ingest_unit('test', p, r, date(2026, 10, 13), date(2023, 10, 13), request_count=0, cached_payload_id=pid)
    assert cached['cachedSourcePayloadId'] == pid and cached['retained'] == 0
    _, changed = sample(close=21)
    with pytest.raises(ValueError, match='cached evidence'):
        repo.ingest_unit('test', p, changed, date(2026, 10, 13), date(2023, 10, 13), request_count=0, cached_payload_id=pid)
