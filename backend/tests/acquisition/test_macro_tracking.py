from datetime import UTC, date, datetime
from app.acquisition.contracts import StateKey
from app.acquisition.repository import StateRepository
from app.macro.jobs.macro_series_state import MacroSeriesStateRepository


def test_macro_success_by_series(factory):
    now = datetime.now(UTC)
    MacroSeriesStateRepository(factory,'test').mark_success('cn.cpi.yoy',checked_at=now,
        active_source='nbs',latest_observed_at=date(2021,1,1),latest_released_at=None,fallback_used=False)
    key = StateKey('cn.cpi.yoy','','series','cn.cpi.yoy')
    with factory() as session:
        assert StateRepository(session).read((key,))[key].scope['source'] == 'nbs'
