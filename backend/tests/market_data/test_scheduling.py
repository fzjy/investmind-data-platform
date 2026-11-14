import importlib
from datetime import datetime

import pytest

from backend.tests.market_data.test_catalog import catalog


def scheduling():
    assert importlib.util.find_spec('app.market.data.scheduling') is not None, 'daily market schedule missing'
    return importlib.import_module('app.market.data.scheduling')


@pytest.mark.parametrize('instant,due,next_run', [
    ('2026-10-16T10:59:59+00:00','2026-10-15T11:00:00+00:00','2026-10-16T11:00:00+00:00'),
    ('2026-10-16T11:00:00+00:00','2026-10-16T11:00:00+00:00','2026-10-17T11:00:00+00:00'),
    ('2026-10-18T12:00:00+00:00','2026-10-18T11:00:00+00:00','2026-10-19T11:00:00+00:00'),
])
def test_market_due_slot_changes_at_1900_shanghai(instant, due, next_run):
    module = scheduling()
    value = catalog().schedule
    assert module.most_recent_market_schedule(datetime.fromisoformat(instant), value).isoformat() == due
    assert module.next_market_schedule(datetime.fromisoformat(instant), value).isoformat() == next_run


@pytest.mark.parametrize('instant,wall,due,next_run', [
    ('2026-11-01T06:45:00+00:00','01:30','2026-11-01T05:30:00+00:00','2026-11-02T06:30:00+00:00'),
    ('2026-03-08T07:15:00+00:00','02:30','2026-03-07T07:30:00+00:00','2026-03-08T07:30:00+00:00'),
])
def test_dst_fold_first_occurrence_and_gap_forward(instant, wall, due, next_run):
    module = scheduling()
    value = catalog().schedule.model_dump(mode='json', by_alias=True)
    value.update(timezone='America/New_York', time=wall)
    schedule = type(catalog().schedule).model_validate(value)
    assert module.most_recent_market_schedule(datetime.fromisoformat(instant), schedule).isoformat() == due
    assert module.next_market_schedule(datetime.fromisoformat(instant), schedule).isoformat() == next_run


def test_daily_schedule_rejects_naive_clock():
    with pytest.raises(ValueError):
        scheduling().most_recent_market_schedule(datetime(2026,10,16), catalog().schedule)
