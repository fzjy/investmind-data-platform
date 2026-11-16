from datetime import UTC, date, datetime

from app.acquisition.contracts import AuditContext, StateKey, StatusQuery
from app.acquisition.service import StatusService
from app.acquisition.tracking import record_terminal
from app.db.models import DailyPrice
from app.stocks.update.validation import validate_response
from backend.tests.stock_update.test_repository import sample
import pytest


def test_explicit_more_rejects_short_stock_response():
    plan, response = sample()
    response = type(response)(fields=response.fields, items=response.items, has_more=True)
    with pytest.raises(ValueError, match='pagination'):
        validate_response(plan, response, date(2026,10,13), date(2026,10,13))


def test_selected_period_requires_matching_proof(factory):
    now = datetime.now(UTC)
    key = StateKey('stock-daily','','security','600519.SH')
    with factory.begin() as session:
        for day in (date(2026,1,10),date(2026,5,10)):
            session.add(DailyPrice(ts_code=key.subject_key,trade_date=day,close=0))
        record_terminal(session,key,started_at=now,checked_at=now,status='success',
            scope={'params':{'start_date':'20260501','end_date':'20260531'}})
    def item(period):
        return StatusService(factory).query(StatusQuery((key.entry_id,),subject_keys=(key.subject_key,),
            context=AuditContext(period=period))).items[0]
    assert item('2026-01-10').availability.kind == 'available'
    assert item('2026-01-10').assessment.status == 'excluded'
    assert item('2026-05-10').assessment.status == 'normal'
    assert item('2026-05-11').assessment.status == 'missing'


def test_month_filters_values_and_requires_proof_for_selected_day():
    from app.acquisition.periods import covers
    from app.acquisition.availability.published import _item
    assert not covers({'params':{'trade_date':'20260102'}},'2026-01-30','2026-01')
    key = StateKey('stock-daily','','security','600519.SH')
    data = {'dailyChart':{'candles':[{'date':'2026-01-13','close':10}]}}
    assert _item(key,'stock-workbench:600519.SH',data,'2026-01').period == '2026-01-13'
