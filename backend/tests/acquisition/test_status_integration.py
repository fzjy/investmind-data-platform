from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from app.acquisition.contracts import StatusQuery
from app.acquisition.service import StatusService
from app.stocks.update.repository import StockUpdateRepository
from backend.tests.stock_update.test_repository import sample


def test_source_to_api_and_repaired_audit(stock_factory):
    factory = stock_factory
    service = StatusService(factory,root=Path(__file__).resolve().parents[3],clock=lambda:datetime.now(UTC))
    query = StatusQuery(('stock-daily',),subject_keys=('600519.SH',))
    store = StockUpdateRepository(factory)
    plan,good = sample()
    day = date(2026,10,13)
    def audit():
        return service.query(query).items[0]
    assert audit().assessment.status == 'missing'
    store.ingest_unit('routine',plan,good,day,day)
    assert audit().assessment.status == 'normal'
    _,bad = sample(symbol='000001.SZ')
    store.ingest_unit('routine',plan,bad,day,day)
    assert audit().assessment.status == 'failed'
    assert audit().availability.kind == 'available'
    other = service.query(replace(query,subject_keys=('000001.SZ',))).items[0]
    assert other.assessment.status == 'missing' and other.evidence.status == 'unrecorded'
    store.ingest_unit('routine',plan,good,day,day)
    assert audit().assessment.status == 'normal'
