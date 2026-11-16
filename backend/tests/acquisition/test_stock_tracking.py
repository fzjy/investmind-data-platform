from datetime import date
from app.acquisition.contracts import StateKey
from app.acquisition.repository import StateRepository
from app.stocks.update.repository import StockUpdateRepository
from backend.tests.stock_update.test_repository import sample


def test_scoped_stock_response(factory):
    repo = StockUpdateRepository(factory)
    day = date(2026,10,13)
    plan, response = sample()
    repo.ingest_unit('test',plan,response,day,day)
    key = StateKey('stock-daily','','security','600519.SH')
    with factory() as session:
        assert StateRepository(session).read((key,))[key].status == 'success'
    plan, response = sample(symbol='000001.SZ')
    repo.ingest_unit('test',plan,response,day,day)
    other = StateKey('stock-daily','','security','000001.SZ')
    with factory() as session:
        records = StateRepository(session).read((key,other))
        assert records[key].status == 'failed' and other not in records
