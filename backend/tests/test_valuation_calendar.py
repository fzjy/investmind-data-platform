from datetime import date
from app.stocks import valuation_calendar
from backend.tests.test_valuation_supply import factory

def test_calendar_requires_every_day_and_rejects_conflicting_open_status():
    rows=[{'exchange':'SSE','cal_date':'20261020','is_open':'1'},{'exchange':'SSE','cal_date':'20261021','is_open':1}]
    assert valuation_calendar.calendar_days(rows,date(2026,10,20),date(2026,10,21))==[date(2026,10,20),date(2026,10,21)]
    import pytest
    with pytest.raises(ValueError):valuation_calendar.calendar_days(rows[:1],date(2026,10,20),date(2026,10,21))
    with pytest.raises(ValueError):valuation_calendar.calendar_days(rows+[dict(rows[0],is_open=0)],date(2026,10,20),date(2026,10,21))


def test_saved_calendar_rejects_changed_batch_source(factory):
    import json,base64,hashlib,pytest
    from datetime import timedelta
    from sqlalchemy import select
    from app.db.models import IngestionBatch
    from app.connectors.tushare.contracts import TushareResponse
    class Client:
        def set_response_observer(self,callback):self.callback=callback
        def call(self,api,params,fields):
            start=date.fromisoformat(params['start_date']);end=date.fromisoformat(params['end_date'])
            items=[['SSE',(start+timedelta(days=i)).strftime('%Y%m%d'),int((start+timedelta(days=i)).weekday()<5)] for i in range((end-start).days+1)]
            data=dict(fields=list(fields),items=items,has_more=False);body=json.dumps(dict(code=0,data=data)).encode()
            self.callback(dict(statusCode=200,credentialsRedacted=False,bodyBase64=base64.b64encode(body).decode(),bodySha256=hashlib.sha256(body).hexdigest(),byteLength=len(body)))
            return TushareResponse.model_validate(data)
    assert valuation_calendar.capture_calendar(factory,Client(),date(2026,11,6),'source',execute=True)['status']=='completed'
    with factory.begin() as s:s.scalar(select(IngestionBatch)).source='changed'
    with pytest.raises(ValueError):valuation_calendar.load_calendar(factory,date(2026,11,6))
