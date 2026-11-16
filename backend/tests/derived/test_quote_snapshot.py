from datetime import date

from app.db.models import AdjustmentFactor, DailyBasic, DailyPrice, Security
from app.stocks.derived.inputs import CoreSnapshotRepository



def test_explicit_quote_snapshot_preserves_latest_values_without_materializing_history(pg_factory):
    factory = pg_factory
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="sample", exchange="SSE"))
        session.flush()
        for day in (22, 23, 24, 25):
            when = date(2026, 8, day)
            session.add(DailyPrice(ts_code="600519.SH", trade_date=when, close=day))
            session.add(DailyBasic(ts_code="600519.SH", trade_date=when, pe=day))
            session.add(AdjustmentFactor(ts_code="600519.SH", trade_date=when, adj_factor=day))
    repository = CoreSnapshotRepository(factory)
    full = repository.load(date(2026, 8, 24))
    assert len(full.daily_prices) == 3
    quote = repository.load(date(2026, 8, 24), market_history=False)
    for rows in (quote.daily_prices, quote.daily_basics, quote.adjustment_factors):
        assert len(rows) == 1 and rows[0].trade_date == date(2026, 8, 24)
    assert quote.daily_prices[0].close == 24
    assert len(repository.load(date(2026, 8, 24)).daily_prices) == 3
