from datetime import date
from app.db.models import IngestionBatch, SourcePayload, ShareholderCount, Security
from app.ingestion.repositories import content_hash
from app.stocks.shareholder_counts import load_shareholder_counts
from app.stocks.shareholder_returns import concentration_periods



def test_raw_history_replays_overwritten_core_and_preserves_same_day_conflicts(pg_factory):
    factory = pg_factory
    code = '600519.SH'
    with factory.begin() as session:
        batch = IngestionBatch(id='counts', source='tushare', dataset='shareholder_count', status='completed', rejected_count=0)
        session.add(Security(ts_code=code, symbol=code[:6], name=code))
        session.add(batch)
        session.flush()
        payload = dict(fields=['ts_code', 'ann_date', 'end_date', 'holder_num'], items=[
            [code, '20250401', '20250331', 100], [code, '20250401', '20250331', 110],
            [code, '20250501', '20250331', 120], ['000001.SZ', '20250401', '20250331', 999]])
        session.add(SourcePayload(batch_id='counts', api_name='stk_holdernumber', request_hash='range', content_hash=content_hash(payload), payload=payload))
        session.add(ShareholderCount(ts_code=code, ann_date=date(2025, 5, 1), end_date=date(2025, 3, 31), holder_num=120))
    with factory() as session:
        rows = load_shareholder_counts(session, [code], date(2025, 4, 15))
        assert {r.holder_num for r in rows} == {100, 110}
        assert concentration_periods(rows, [], date(2025, 4, 15))[0]['holderCount'] is None
        rows = load_shareholder_counts(session, [code], date(2025, 5, 2))
        assert concentration_periods(rows, [], date(2025, 5, 2))[0]['holderCount'] == 120
