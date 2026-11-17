"""共享来源日历：只替换 HTTP，验证真实 provider/runtime/scheduler 的边界。"""
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.connectors.macro.contracts import MacroFetchWindow
from app.connectors.macro import fred
from app.connectors.macro.transport import MacroHttpTransport
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


@pytest.mark.parametrize('stamp,day', [
    ('2026-10-11T02:00:00+00:00', '2026-10-10'),
    ('2026-10-11T10:00:00+08:00', '2026-10-10'),
    ('2026-10-11T12:00:00+00:00', '2026-10-11'),
    ('2026-01-11T05:30:00+00:00', '2026-01-10'),
    ('2026-03-08T07:59:00+00:00', '2026-03-08'),
    ('2026-11-01T06:30:00+00:00', '2026-11-01'),
])
@pytest.mark.parametrize('sid', ['us.cpi.yoy', 'us.labor.nonfarm_payroll_change', 'us.rate.treasury_10y'])
def test_shared_calendar_dates_values_and_clipped_validity(stamp, day, sid):
    instant = datetime.fromisoformat(stamp)
    seen = []
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(sid)
    period = '2025-12-01' if instant.month == 1 else '2025-08-01'
    base = '2024-12-01' if instant.month == 1 else '2024-08-01'
    if sid.endswith('payroll_change'):
        base = '2025-11-01' if instant.month == 1 else '2025-07-01'

    def handle(request):
        q = dict(request.url.params)
        seen.append(q)
        assert q['realtime_end'] == day
        if request.url.path.endswith('vintagedates'):
            return httpx.Response(200, json={'count': 1, 'offset': 0, 'vintage_dates': [day]})
        assert q['observation_end'] == day
        rows = [{'date': p, 'value': v, 'realtime_start': day, 'realtime_end': day}
                for p, v in ((base, '100'), (period, '105'))]
        if series.frequency == 'daily': rows = rows[1:]
        return httpx.Response(200, json={'count': len(rows), 'offset': 0, 'observations': rows})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        http = MacroHttpTransport(client=client, max_attempts=1, backoff_seconds=(0,))
        result = fred.CalendarFredMacroProvider('controlled-key', transport=http).fetch(
            series, series.sources[0], MacroFetchWindow(date(2025, 1, 1), instant.date(), instant))
    assert result.status == 'success'
    assert result.request_count == len(seen) == (1 if series.frequency == 'daily' else 2)
    assert len(result.observations) == 1
    row = result.observations[0]
    assert row.value == Decimal('105' if series.frequency == 'daily' else '5')
    assert row.valid_until is None
    if series.frequency == 'daily': assert row.released_at is None and row.revision is None
    assert result.raw_payload['sourceCalendar']['end'] == day


@pytest.mark.parametrize('stamp', ['2026-10-10T16:00:00+00:00', '2026-10-11T00:00:00+08:00'])
@pytest.mark.parametrize('sid', ['us.cpi.yoy', 'us.labor.nonfarm_payroll_change', 'us.rate.treasury_10y'])
def test_unclipped_equivalent_instants_use_identical_utc_cutoff(stamp, sid):
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(sid)
    calls = []
    def handle(request):
        q = dict(request.url.params)
        calls.append(q)
        assert q['realtime_end'] == '2026-10-10'
        if request.url.path.endswith('vintagedates'):
            return httpx.Response(200, json={'vintage_dates': ['2026-10-10']})
        assert q['observation_end'] == '2026-10-10'
        base = '2025-07-01' if sid.endswith('payroll_change') else '2024-08-01'
        rows = [{'date': p, 'value': v, 'realtime_start': '2026-10-10', 'realtime_end': '2026-10-10'}
                for p, v in ((base, '100'), ('2025-08-01', '105'))]
        if series.frequency == 'daily': rows = rows[1:]
        return httpx.Response(200, json={'observations': rows})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = fred.CalendarFredMacroProvider('controlled-key', transport=MacroHttpTransport(
            client=client, max_attempts=1, backoff_seconds=(0,))).fetch(series, series.sources[0],
            MacroFetchWindow(date(2025, 1, 1), date(2026, 10, 11), datetime.fromisoformat(stamp)))
    assert result.status == 'success'
    assert result.request_count == len(calls) == (1 if series.frequency == 'daily' else 2)
    assert result.raw_payload['sourceCalendar'] == {
        'timezone': 'America/Chicago', 'end': '2026-10-10',
        'observationEnd': '2026-10-10', 'asOf': '2026-10-10T16:00:00+00:00'}
    assert result.observations[0].value == Decimal('105' if series.frequency == 'daily' else '5')
    assert result.observations[0].valid_until is None


def test_shared_calendar_empty_source_day_never_calls_http():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('us.cpi.yoy')
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail('reversed source window requested'))) as client:
        result = fred.CalendarFredMacroProvider('controlled-key', transport=MacroHttpTransport(
            client=client, max_attempts=1, backoff_seconds=(0,))).fetch(series, series.sources[0],
            MacroFetchWindow(date(2026, 10, 11), date(2026, 10, 11), datetime(2026, 10, 11, 2, tzinfo=UTC)))
    assert result.status == 'no_change' and result.request_count == 0 and result.observations == ()


def test_historical_observation_end_does_not_hide_newer_revisions():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('us.cpi.yoy')
    def handle(request):
        assert request.url.params['realtime_end'] == '2026-10-10'
        if request.url.path.endswith('vintagedates'):
            return httpx.Response(200, json={'vintage_dates': ['2026-10-02']})
        assert request.url.params['observation_end'] == '2026-08-31'
        return httpx.Response(200, json={'observations': [
            {'date': p, 'value': v, 'realtime_start': '2026-10-02', 'realtime_end': '2026-10-10'}
            for p, v in (('2025-08-01', '125'), ('2026-08-01', '105'))]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = fred.CalendarFredMacroProvider('controlled-key', transport=MacroHttpTransport(
            client=client, max_attempts=1, backoff_seconds=(0,))).fetch(series, series.sources[0],
            MacroFetchWindow(date(2026, 8, 1), date(2026, 8, 31), datetime(2026, 10, 11, 2, tzinfo=UTC)))
    assert result.status == 'success' and result.observations[0].value == Decimal('-16')
    assert result.observations[0].released_at == datetime(2026, 10, 2, tzinfo=UTC)
    assert result.observations[0].valid_until is None


def test_generic_runtime_uses_calendar_for_actual_request(pg_url):
    from app.macro.jobs.macro_sync_runtime import open_macro_sync_runtime
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('us.rate.treasury_10y')
    def handle(request):
        assert request.url.params['realtime_start'] == request.url.params['realtime_end'] == '2026-10-10'
        return httpx.Response(200, json={'count': 1, 'offset': 0, 'observations': [
            {'date': '2026-10-09', 'value': '4.2', 'realtime_start': '2026-10-10', 'realtime_end': '2026-10-10'}]})
    with open_macro_sync_runtime(Settings(database_url=pg_url, fred_api_key='controlled-key'),
                                 transport=httpx.MockTransport(handle)) as runtime:
        result = runtime.job._registry.require('fred').fetch(series, series.sources[0],
            MacroFetchWindow(date(2026, 10, 1), date(2026, 10, 11), datetime(2026, 10, 11, 2, tzinfo=UTC)))
    assert result.status == 'success' and result.request_count == 1
    assert result.observations[0].value == Decimal('4.2')


def test_shared_calendar_keeps_real_earlier_revision_end():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('us.cpi.yoy')
    def handle(request):
        if request.url.path.endswith('vintagedates'):
            return httpx.Response(200, json={'vintage_dates': ['2026-09-01', '2026-10-02']})
        return httpx.Response(200, json={'observations': [
            {'date': '2025-08-01', 'value': '100', 'realtime_start': '2026-09-01', 'realtime_end': '2026-10-01'},
            {'date': '2025-08-01', 'value': '125', 'realtime_start': '2026-10-02', 'realtime_end': '2026-10-10'},
            {'date': '2026-08-01', 'value': '105', 'realtime_start': '2026-09-01', 'realtime_end': '2026-10-10'}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = fred.CalendarFredMacroProvider('controlled-key', transport=MacroHttpTransport(
            client=client, max_attempts=1, backoff_seconds=(0,))).fetch(series, series.sources[0],
            MacroFetchWindow(date(2026, 8, 1), date(2026, 10, 11), datetime(2026, 10, 11, 2, tzinfo=UTC)))
    assert [v.value for v in result.observations] == [Decimal('5'), Decimal('-16')]
    assert [v.valid_until for v in result.observations] == [datetime(2026, 10, 2, tzinfo=UTC), None]


def test_rates_runtime_cross_day_persists_real_values_without_publishing(pg_factory, pg_schema_url):
    from sqlalchemy import select
    from app.db.models import MacroObservation, MacroSeriesState, PublishedSnapshot
    from app.macro.jobs.macro_sync_planning import MacroSyncOptions
    from app.macro.jobs.us_rates_sync_runtime import open_us_rates_sync_runtime, SERIES_IDS
    from tests.jobs.test_us_rates_sync import seed
    factory = pg_factory
    engine = factory.kw['bind']
    seed(factory)
    now = datetime(2026, 10, 11, 2, tzinfo=UTC)
    calls = []
    def handle(request):
        q = request.url.params
        calls.append(dict(q))
        assert q['observation_end'] == q['realtime_start'] == q['realtime_end'] == '2026-10-10'
        return httpx.Response(200, json={'count': 1, 'offset': 0, 'observations': [
            {'date': '2026-10-09', 'value': '3.8' if q['series_id'] == 'DFF' else '5.0',
             'realtime_start': '2026-10-10', 'realtime_end': '2026-10-10'}]})
    try:
        with open_us_rates_sync_runtime(Settings(database_url=pg_schema_url, fred_api_key='controlled-key'),
                                        transport=httpx.MockTransport(handle)) as runtime:
            runtime.job._clock = lambda: now
            result = runtime.job.run(MacroSyncOptions(now, 10, runtime.catalog.catalog_version,
                runtime.catalog.catalog_hash, force=True, mode='live', series_ids=SERIES_IDS, publish=False))
        assert result.status == 'completed' and result.completed == result.row_count == result.request_count == len(calls) == 2
        with factory() as s:
            assert {r.series_id: r.value for r in s.scalars(select(MacroObservation).where(
                MacroObservation.observed_at == date(2026, 10, 9)))} == {SERIES_IDS[0]: Decimal('3.8'), SERIES_IDS[1]: Decimal('5')}
            states = s.scalars(select(MacroSeriesState).where(MacroSeriesState.series_id.in_(SERIES_IDS))).all()
            assert all(r.last_success_at.replace(tzinfo=UTC) == now and not r.stale for r in states)
            assert {r.version for r in s.scalars(select(PublishedSnapshot))} == {'old-published'}
    finally: engine.dispose()
