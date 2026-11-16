from datetime import UTC, date, datetime
import pytest
from app.db.models import DailyPrice, PageSnapshot, PublishedSnapshot
from app.acquisition.contracts import AuditContext, StateKey
from app.acquisition.availability.stored import finite, read_stored
from app.acquisition.availability.published import read_published


@pytest.mark.parametrize(('value','expected'),[(0,True),(True,False),('NaN',False),(float('inf'),False),('0.4',True)])
def test_finite_numbers(value, expected):
    assert finite(value) is expected


def test_stored_row_does_not_prove_published_value(factory):
    key = StateKey('stock-daily','','security','600519.SH')
    now = datetime.now(UTC)
    with factory.begin() as s:
        s.add(DailyPrice(ts_code='600519.SH',trade_date=date(2022,1,1),close=0))
        product = 'stock-workbench:600519.SH'
        s.add(PublishedSnapshot(product=product,version='v1',as_of=now,payload={'chart':[]}))
        assert read_stored(s,(key,),now=now)[key].kind == 'available'
        assert read_published(s,(key,),AuditContext('published',product,'v1'))[key].kind == 'missing'
