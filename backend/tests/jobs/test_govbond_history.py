"""测试真实月度队列和 raw/core；仅外部 HTTP 用完整协议 fixture 替代。"""
from datetime import UTC, date, datetime
from importlib import import_module, util
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.connectors.macro.contracts import ProviderTransientError
from app.connectors.macro.transport import JsonTransportResponse
from app.db.models import JobRun, MacroObservation, SourcePayload
from app.jobs.cursors import SqlJobCursorRepository
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSourceDefinition

NOW = datetime(2026, 10, 7, 15, tzinfo=UTC)
SOURCE = {
    'provider': 'chinamoney', 'priority': 1, 'series': 'CGB-10Y',
    'api': 'SddsIntrRateGovYldHis', 'field': 'tenRate', 'transform': 'identity',
    'sourceUrl': 'https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/SddsIntrRateGovYldHis',
    'termsUrl': 'https://www.chinamoney.com.cn/chinese/mtsjfu/', 'accessMode': 'public_page',
    'displayAllowed': True, 'responseFields': ['dateString', 'tenRate'], 'params': {'maxPages': '3'},
}


class Transport:
    def __init__(self, *, failures=None, empty=False, rows=1):
        self.failures = failures or {}
        self.empty, self.rows, self.calls = empty, rows, []

    def post_json(self, url, payload):
        assert payload == {}
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.calls.append(q)
        key = (q['startDate'], int(q['pageNum']))
        if key in self.failures:
            raise self.failures[key]
        total = 0 if self.empty else self.rows
        page = int(q['pageNum'])
        rows = [{'dateString': q['startDate'][:8] + f'{day:02}', 'tenRate': '1.6750'}
                for day in range((page - 1) * 15 + 1, min(page * 15, total) + 1)]
        return JsonTransportResponse({
            'head': {'version': '2.0', 'provider': 'CWAP', 'rep_code': '200'},
            'data': {'total': total, 'pageTotal': (total + 14) // 15, 'pageNum': page - 1,
                     'pageSize': 15, 'startDateCN': q['startDate'], 'endDateCN': q['endDate'], 'flagMessage': ''},
            'records': rows,
        }, 1)


@pytest.fixture
def setup(pg_factory):
    if util.find_spec('app.macro.jobs.govbond_history') is None:
        pytest.skip('首个 RED 检查确认月度回填入口尚未实现')
    module = import_module('app.macro.jobs.govbond_history')
    repo = import_module('app.macro.jobs.govbond_history_repository')
    engine, factory = pg_factory.kw["bind"], pg_factory
    cursors = SqlJobCursorRepository(factory, job_name='backfill-govbond-history')
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    lock = PostgresAdvisoryJobLock(engine)
    def job(transport, *, clock=lambda: NOW, source=None):
        return module.GovbondHistoryJob(catalog=catalog, source=source or MacroSourceDefinition.model_validate(SOURCE),
            transport=transport, cursors=cursors, repository=repo.GovbondHistoryRepository(factory),
            lock=lock, clock=clock)
    yield factory, cursors, job, lock
    engine.dispose()


def run(job, **changes):
    return job.run(**{'as_of': NOW, 'history_years': 1, 'end': None, 'request_limit': 3, **changes})


def test_monthly_backfill_entry_is_available():
    assert util.find_spec('app.macro.jobs.govbond_history') is not None, '缺少国债月度回填实现'


def test_budget_and_restart(setup):
    factory, cursors, job, _ = setup
    first = Transport(rows=16)
    report = run(job(first))
    assert report['requestCount'] == 2 and report['rowCount'] == 16
    assert report['start'] == '2025-10-01' and report['end'] == '2026-09-30'
    assert report['completedMonths'] == 1 and report['pendingMonths'] == 11
    second = Transport()
    report = run(job(second))
    assert second.calls[0]['startDate'] == '2026-08-01'
    assert report['completedMonths'] == 2 and report['requestCount'] == 1
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 17
        assert len(session.scalars(select(JobRun)).all()) == 2


def test_cross_month_window_frozen_and_capture_is_actual(setup):
    factory, _, job, _ = setup
    run(job(Transport()))
    later = datetime(2026, 11, 9, 10, tzinfo=UTC)
    report = run(job(Transport(), clock=lambda: later), as_of=later)
    assert report['end'] == '2026-09-30'
    assert report['asOf'] == '2026-10-07T15:00:00+00:00'
    with factory() as session:
        row = session.scalar(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 8, 1)))
        assert row.released_at.replace(tzinfo=UTC) == later


def test_failed_month_rotates(setup):
    _, _, job, _ = setup
    failed = Transport(failures={('2026-09-01', 1): ProviderTransientError('HTTP_REQUEST_FAILED', request_count=1)})
    report = run(job(failed), request_limit=4)
    assert report['requestCount'] == 2 and report['completedMonths'] == 1
    assert report['failures'][0]['month'] == '2026-09'
    second = Transport()
    run(job(second))
    assert second.calls[0]['startDate'] == '2026-07-01'


def test_empty_month_is_pending(setup):
    factory, _, job, _ = setup
    report = run(job(Transport(empty=True)))
    assert report['status'] == 'partial' and report['completedMonths'] == 0
    assert report['failures'] == [{'month': '2026-09', 'code': 'GOVBOND_HISTORY_EMPTY_MONTH'}]
    with factory() as session:
        assert session.scalars(select(MacroObservation)).all() == []
        assert len(session.scalars(select(SourcePayload)).all()) == 1


def test_second_page_failure_has_no_core(setup):
    factory, _, job, _ = setup
    report = run(job(Transport(rows=16, failures={('2026-09-01', 2): ProviderTransientError('HTTP_REQUEST_FAILED', request_count=1)})))
    assert report['rowCount'] == 0 and report['requestCount'] == 2
    with factory() as session:
        assert session.scalars(select(MacroObservation)).all() == []
        raw = session.scalar(select(SourcePayload))
        assert len(raw.payload['data']['pages']) == 1


def test_interrupt_after_core_is_idempotent(setup, monkeypatch):
    factory, cursors, job, _ = setup
    original = cursors.save
    def interrupt(key, value):
        if any(e['status'] == 'done' for e in value['entries']):
            raise KeyboardInterrupt()
        original(key, value)
    monkeypatch.setattr(cursors, 'save', interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(job(Transport()))
    monkeypatch.setattr(cursors, 'save', original)
    report = run(job(Transport()))
    assert report['rowCount'] == 0 and report['completedMonths'] == 1
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 1


@pytest.mark.parametrize('mutation', ['core_missing', 'core_value', 'core_unit', 'raw_content', 'raw_missing'])
def test_tampered_done_evidence(setup, mutation):
    factory, cursors, job, _ = setup
    current = job(Transport())
    run(current)
    before = cursors.load('govbond-history-v1')
    with factory.begin() as session:
        core = session.scalar(select(MacroObservation))
        raw = session.scalar(select(SourcePayload))
        if mutation == 'core_missing':
            session.delete(core)
        elif mutation == 'core_value':
            core.value = 9
        elif mutation == 'core_unit':
            core.unit = 'bp'
        elif mutation == 'raw_missing':
            session.delete(raw)
        else:
            raw.payload = {**raw.payload, 'status': 'failed'}
    report = current.status()
    assert report['completedMonths'] == 0 and report['pendingMonths'] == 12
    assert report['failures'] == [{'month': '2026-09', 'code': 'GOVBOND_HISTORY_EVIDENCE_INVALID'}]
    assert cursors.load('govbond-history-v1') == before


@pytest.mark.parametrize('changes', [{'history_years': 2}, {'end': date(2026, 8, 31)}])
def test_config_mismatch(setup, changes):
    _, cursors, job, _ = setup
    run(job(Transport()))
    before = cursors.load('govbond-history-v1')
    with pytest.raises(ValueError, match='^GOVBOND_HISTORY_CONFIG_MISMATCH$'):
        run(job(Transport()), **changes)
    assert cursors.load('govbond-history-v1') == before


def test_locked_does_not_write(setup):
    factory, cursors, job, lock = setup
    assert lock.try_acquire(data_publication_lock_key())
    try:
        assert run(job(Transport()))['status'] == 'locked'
        assert cursors.load('govbond-history-v1') is None
        with factory() as session:
            assert session.scalars(select(JobRun)).all() == []
    finally:
        lock.release(data_publication_lock_key())


def test_completed_status_never_refetches_done_months(setup):
    _, _, job, _ = setup
    report = run(job(Transport()), request_limit=100)
    assert report['status'] == 'completed' and report['completedMonths'] == 12
    transport = Transport()
    report = run(job(transport))
    assert report['requestCount'] == 0 and transport.calls == []


def test_valid_done_survives_json_utc_serialization(setup):
    _, cursors, job, _ = setup
    current = job(Transport())
    assert run(current)['completedMonths'] == 1
    assert cursors.load('govbond-history-v1')['entries'][0]['points'][0]['released_at'] == '2026-10-07T15:00:00Z'
    assert current.status()['completedMonths'] == 1


@pytest.mark.parametrize('changes', [{'request_limit': 2}, {'end': date(2026, 9, 29)},
    {'end': date(2026, 10, 31)}, {'as_of': datetime(2026, 10, 7)}, {'history_years': True}])
def test_invalid_configuration_has_no_side_effects(setup, changes):
    _, cursors, job, _ = setup
    with pytest.raises(ValueError, match='^GOVBOND_HISTORY_CONFIG_INVALID$'):
        run(job(Transport()), **changes)
    assert cursors.load('govbond-history-v1') is None


def test_page_one_is_durable_if_page_two_is_interrupted(setup):
    factory, cursors, job, _ = setup
    with pytest.raises(KeyboardInterrupt):
        run(job(Transport(rows=16, failures={('2026-09-01', 2): KeyboardInterrupt()})))
    with factory() as session:
        raw = session.scalar(select(SourcePayload))
        assert raw is not None, '已接收第一页必须先落地 raw'
        assert len(raw.payload['data']['pages']) == 1
        assert len(raw.payload['data']['pages'][0]['response']['records']) == 15
        assert session.scalars(select(MacroObservation)).all() == []
    assert cursors.load('govbond-history-v1')['entries'][0]['status'] == 'pending'


def test_checkpoint_error_text_is_not_public(setup):
    _, cursors, job, _ = setup
    run(job(Transport(empty=True)))
    state = cursors.load('govbond-history-v1')
    state['entries'][-1]['error_code'] = 'postgres://private-user:private-password@host/db'
    cursors.save('govbond-history-v1', state)
    report = job(None).status()
    assert 'private-password' not in str(report)
    assert report['failures'] == [{'month': '2026-09', 'code': 'GOVBOND_HISTORY_FETCH_FAILED'}]


def test_same_revision_second_repair_selects_exact_released_identity(setup):
    factory, _, job, _ = setup
    # 每次运行一轮，先使全部月窗 done，之后仅被修改的9月需要恢复。
    run(job(Transport()), request_limit=100)
    for day in (8, 9):
        with factory.begin() as session:
            row = session.scalar(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 9, 1))
                .order_by(MacroObservation.released_at.desc()))
            row.value = 9
        later = NOW.replace(day=day)
        result = run(job(Transport(), clock=lambda: later), as_of=later, request_limit=100)
        assert result['status'] == 'completed', result
        assert job(None).status()['completedMonths'] == 12
