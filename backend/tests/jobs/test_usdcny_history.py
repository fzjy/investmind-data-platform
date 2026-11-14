"""测试真实月度队列和 raw/core；仅外部 HTTP 用完整协议 fixture 替代。"""
from datetime import UTC, date, datetime, timedelta, timezone
from importlib import import_module, util
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from app.connectors.macro.contracts import ProviderTransientError
from app.connectors.macro.transport import JsonTransportResponse
from app.db.base import Base, ExternalBase, MartBase
from app.db.models import JobRun, MacroObservation, SourcePayload, MacroSeriesState, PublishedSnapshot, JobFailure
from app.db.session import create_engine_and_session
from app.jobs.cursors import SqlJobCursorRepository
from app.jobs.locks import ProcessLocalJobLock, data_publication_lock_key
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog, MacroSourceDefinition

NOW = datetime(2026, 10, 9, 15, tzinfo=UTC)
SOURCE = {
    'provider': 'chinamoney', 'priority': 1, 'series': 'USD/CNY',
    'api': 'CcprHisNew', 'field': 'values', 'transform': 'identity',
    'sourceUrl': 'https://www.chinamoney.com.cn/ags/ms/cm-u-bk-ccpr/CcprHisNew',
    'termsUrl': 'https://www.chinamoney.com.cn/chinese/mtsjfu/', 'accessMode': 'public_page',
    'displayAllowed': True, 'responseFields': ['date', 'values'], 'params': {'maxPages': '4'},
}


class Transport:
    def __init__(self, *, failures=None, empty=False, rows=1):
        self.failures = failures or {}
        self.empty, self.rows, self.calls = empty, rows, []

    def post_json(self, url, payload):
        from backend.tests.connectors.macro.test_chinamoney_usdcny import CURRENCIES
        assert payload == {}
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert urlsplit(url).path == '/ags/ms/cm-u-bk-ccpr/CcprHisNew'
        assert q['currency'] == 'USD/CNY' and q['pageSize'] == '10'
        self.calls.append(q)
        key = (q['startDate'], int(q['pageNum']))
        if key in self.failures:
            raise self.failures[key]
        total = 0 if self.empty else self.rows
        page = int(q['pageNum'])
        records = [{'date': (date.fromisoformat(q['endDate']) - timedelta(days=i)).isoformat(),
                    'values': ['6.7008']} for i in range((page - 1) * 10, min(page * 10, total))]
        return JsonTransportResponse({
            'head': {'version': '2.0', 'provider': 'CWAP', 'req_code': '', 'rep_code': '200',
                     'rep_message': '', 'ts': 1791544829822, 'producer': '',
                     'tstext': '2026-10-09 19:20:29'},
            'data': {'head': CURRENCIES.copy(), 'total': total, 'pageTotal': (total + 9) // 10,
                     'searchlist': ['USD/CNY'], 'currency': 'USD/CNY', 'pageNum': page,
                     'pageSize': 10, 'startDate': q['startDate'], 'endDate': q['endDate'], 'flagMessage': ''},
            'records': records,
        }, 1)


@pytest.fixture
def setup():
    # RED阶段使用现有真实月度engine展示不支持USD合同；独立入口assert保证fallback不能冒充完成。
    has_usdcny = util.find_spec('app.macro.jobs.usdcny_history') is not None
    module = import_module('app.macro.jobs.usdcny_history' if has_usdcny else 'app.macro.jobs.govbond_history')
    repo = import_module('app.macro.jobs.usdcny_history_repository' if has_usdcny else 'app.macro.jobs.govbond_history_repository')
    job_type = module.UsdcnyHistoryJob if has_usdcny else module.GovbondHistoryJob
    repo_type = repo.UsdcnyHistoryRepository if has_usdcny else repo.GovbondHistoryRepository
    engine, factory = create_engine_and_session('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(engine)
    ExternalBase.metadata.create_all(engine)
    MartBase.metadata.create_all(engine)
    cursors = SqlJobCursorRepository(factory, job_name='backfill-usdcny-history')
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    lock = ProcessLocalJobLock()
    def job(transport, *, clock=lambda: NOW, source=None):
        return job_type(catalog=catalog, source=source or MacroSourceDefinition.model_validate(SOURCE),
            transport=transport, cursors=cursors, repository=repo_type(factory),
            lock=lock, clock=clock)
    yield factory, cursors, job, lock
    engine.dispose()


def run(job, **changes):
    return job.run(**{'as_of': NOW, 'history_years': 1, 'end': None, 'request_limit': 4, **changes})


def test_monthly_backfill_entry_is_available():
    assert util.find_spec('app.macro.jobs.usdcny_history') is not None, '缺少USD/CNY月度回填实现'


def test_budget_and_restart(setup):
    factory, cursors, job, _ = setup
    first = Transport(rows=11)
    report = run(job(first))
    assert report['requestCount'] == 2 and report['rowCount'] == 11
    assert report['start'] == '2025-10-01' and report['end'] == '2026-09-30'
    assert report['completedMonths'] == 1 and report['pendingMonths'] == 11
    second = Transport()
    report = run(job(second))
    assert second.calls[0]['startDate'] == '2026-08-01'
    assert report['completedMonths'] == 2 and report['requestCount'] == 1
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 12
        assert len(session.scalars(select(JobRun)).all()) == 2


def test_cross_month_window_frozen_and_capture_is_actual(setup):
    factory, _, job, _ = setup
    run(job(Transport()))
    later = datetime(2026, 11, 9, 10, tzinfo=UTC)
    report = run(job(Transport(), clock=lambda: later), as_of=later)
    assert report['end'] == '2026-09-30'
    assert report['asOf'] == '2026-10-09T15:00:00+00:00'
    with factory() as session:
        row = session.scalar(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 8, 31)))
        assert row.released_at.replace(tzinfo=UTC) == later


def test_failed_month_rotates(setup):
    _, _, job, _ = setup
    failed = Transport(failures={('2026-09-01', 1): ProviderTransientError('HTTP_REQUEST_FAILED', request_count=1)})
    report = run(job(failed), request_limit=5)
    assert report['requestCount'] == 2 and report['completedMonths'] == 1
    assert report['failures'][0]['month'] == '2026-09'
    second = Transport()
    run(job(second))
    assert second.calls[0]['startDate'] == '2026-07-01'


def test_empty_month_is_pending(setup):
    factory, _, job, _ = setup
    report = run(job(Transport(empty=True)))
    assert report['status'] == 'partial' and report['completedMonths'] == 0
    assert report['failures'] == [{'month': '2026-09', 'code': 'USDCNY_HISTORY_EMPTY_MONTH'}]
    with factory() as session:
        assert session.scalars(select(MacroObservation)).all() == []
        assert len(session.scalars(select(SourcePayload)).all()) == 1


def test_second_page_failure_has_no_core(setup):
    factory, _, job, _ = setup
    report = run(job(Transport(rows=11, failures={('2026-09-01', 2): ProviderTransientError('HTTP_REQUEST_FAILED', request_count=1)})))
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
    before = cursors.load('usdcny-history-v1')
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
    assert report['failures'] == [{'month': '2026-09', 'code': 'USDCNY_HISTORY_EVIDENCE_INVALID'}]
    assert cursors.load('usdcny-history-v1') == before


@pytest.mark.parametrize('changes', [{'history_years': 2}, {'end': date(2026, 8, 31)}])
def test_config_mismatch(setup, changes):
    _, cursors, job, _ = setup
    run(job(Transport()))
    before = cursors.load('usdcny-history-v1')
    with pytest.raises(ValueError, match='^USDCNY_HISTORY_CONFIG_MISMATCH$'):
        run(job(Transport()), **changes)
    assert cursors.load('usdcny-history-v1') == before


def test_locked_does_not_write(setup):
    factory, cursors, job, lock = setup
    assert lock.try_acquire(data_publication_lock_key())
    try:
        assert run(job(Transport()))['status'] == 'locked'
        assert cursors.load('usdcny-history-v1') is None
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
    assert cursors.load('usdcny-history-v1')['entries'][0]['points'][0]['released_at'] == '2026-10-09T15:00:00Z'
    assert current.status()['completedMonths'] == 1


@pytest.mark.parametrize('changes', [{'request_limit': 3}, {'end': date(2026, 9, 29)},
    {'end': date(2026, 10, 31)}, {'as_of': datetime(2026, 10, 7)}, {'history_years': True}])
def test_invalid_configuration_has_no_side_effects(setup, changes):
    _, cursors, job, _ = setup
    with pytest.raises(ValueError, match='^USDCNY_HISTORY_CONFIG_INVALID$'):
        run(job(Transport()), **changes)
    assert cursors.load('usdcny-history-v1') is None


def test_page_one_is_durable_if_page_two_is_interrupted(setup):
    factory, cursors, job, _ = setup
    with pytest.raises(KeyboardInterrupt):
        run(job(Transport(rows=11, failures={('2026-09-01', 2): KeyboardInterrupt()})))
    with factory() as session:
        raw = session.scalar(select(SourcePayload))
        assert raw is not None, '已接收第一页必须先落地 raw'
        assert len(raw.payload['data']['pages']) == 1
        assert len(raw.payload['data']['pages'][0]['response']['records']) == 10
        assert session.scalars(select(MacroObservation)).all() == []
    assert cursors.load('usdcny-history-v1')['entries'][0]['status'] == 'pending'


def test_checkpoint_error_text_is_not_public(setup):
    _, cursors, job, _ = setup
    run(job(Transport(empty=True)))
    state = cursors.load('usdcny-history-v1')
    state['entries'][-1]['error_code'] = 'postgres://private-user:private-password@host/db'
    cursors.save('usdcny-history-v1', state)
    report = job(None).status()
    assert 'private-password' not in str(report)
    assert report['failures'] == [{'month': '2026-09', 'code': 'USDCNY_HISTORY_FETCH_FAILED'}]


def test_same_revision_second_repair_selects_exact_released_identity(setup):
    factory, _, job, _ = setup
    # 每次运行一轮，先使全部月窗 done，之后仅被修改的9月需要恢复。
    run(job(Transport()), request_limit=100)
    for day in (10, 11):
        with factory.begin() as session:
            row = session.scalar(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 9, 30))
                .order_by(MacroObservation.released_at.desc()))
            row.value = 9
        later = NOW.replace(day=day)
        result = run(job(Transport(), clock=lambda: later), as_of=later, request_limit=100)
        assert result['status'] == 'completed', result
        assert job(None).status()['completedMonths'] == 12

def test_four_page_budget_never_overruns(setup):
    _, _, job, _ = setup
    transport = Transport(rows=31)
    report = run(job(transport), end=date(2026, 8, 31))
    assert report['completedMonths'] == 1 and report['confirmedObservations'] == 31
    assert report['requestCount'] == len(transport.calls) == 4


def test_history_before_public_source_start_fails_before_writes(setup):
    factory, cursors, job, _ = setup
    transport = Transport()
    with pytest.raises(ValueError, match='^USDCNY_HISTORY_CONFIG_INVALID$'):
        run(job(transport), history_years=30)
    assert transport.calls == [] and cursors.load('usdcny-history-v1') is None
    with factory() as session:
        assert session.scalars(select(JobRun)).all() == []


def test_three_old_namespaces_and_current_products_untouched(setup):
    factory, _, job, _ = setup
    old = [('backfill-macro-history', 'nbs-history-v1'),
           ('backfill-govbond-history', 'govbond-history-v1'),
           ('backfill-dr007-history', 'dr007-history-v1')]
    for name, key in old:
        SqlJobCursorRepository(factory, job_name=name).save(key, {'protected': name})
    assert run(job(Transport()))['completedMonths'] == 1
    for name, key in old:
        assert SqlJobCursorRepository(factory, job_name=name).load(key) == {'protected': name}
    with factory() as session:
        assert session.scalars(select(MacroSeriesState)).all() == []
        assert session.scalars(select(PublishedSnapshot)).all() == []
        assert session.scalars(select(JobFailure)).all() == []


def test_source_identity_change_rejected_without_cursor_write(setup):
    _, cursors, job, _ = setup
    assert run(job(Transport()))['completedMonths'] == 1
    before = cursors.load('usdcny-history-v1')
    changed = MacroSourceDefinition.model_validate({**SOURCE, 'displayAllowed': False})
    with pytest.raises(ValueError, match='^USDCNY_HISTORY_CONFIG_MISMATCH$'):
        run(job(Transport(), source=changed))
    assert cursors.load('usdcny-history-v1') == before


@pytest.mark.parametrize('same_version', [False, True])
def test_catalog_identity_change_rejected_without_cursor_write_or_http(setup, same_version, tmp_path):
    _, cursors, job, _ = setup
    current = job(Transport())
    run(current)
    before = cursors.load('usdcny-history-v1')
    if same_version:
        import json
        payload = json.loads(DEFAULT_MACRO_CATALOG_FILE.read_text(encoding='utf-8'))
        payload['defaults']['requestTimeoutSeconds'] = 37
        path = tmp_path / 'macro-series.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        current.catalog = MacroSeriesCatalog.load(path)
    else:
        current.catalog = current.catalog.model_copy(update={'catalog_version': 'unproven-v10'})
    transport = Transport()
    current.transport = transport
    with pytest.raises(ValueError, match='^USDCNY_HISTORY_CONFIG_MISMATCH$'):
        run(current)
    assert transport.calls == [] and cursors.load('usdcny-history-v1') == before


def test_earliest_full_month_is_accepted_without_inventing_old_release(setup):
    factory, _, job, _ = setup
    report = run(job(Transport()), end=date(2007, 1, 31))
    assert report['start'] == '2006-02-01' and report['end'] == '2007-01-31'
    assert report['completedMonths'] == 1
    with factory() as session:
        row = session.scalar(select(MacroObservation))
        assert row.observed_at == date(2007, 1, 31)
        assert row.released_at.replace(tzinfo=UTC) == NOW


@pytest.mark.parametrize('offset', [8, -5])
def test_non_utc_clock_confirms_and_resumes_without_refetch(setup, offset):
    factory, cursors, job, _ = setup
    local = NOW.astimezone(timezone(timedelta(hours=offset)))
    first = Transport(rows=11)
    result = run(job(first, clock=lambda: local), as_of=local)
    assert result['completedMonths'] == 1, result
    assert job(None).status()['confirmedObservations'] == 11
    state = cursors.load('usdcny-history-v1')
    assert datetime.fromisoformat(state['as_of']) == NOW
    with factory() as session:
        assert all(row.captured_at.replace(tzinfo=UTC) == NOW
                   for row in session.scalars(select(SourcePayload)))
        assert all(row.released_at.replace(tzinfo=UTC) == NOW
                   for row in session.scalars(select(MacroObservation)))
    later = local + timedelta(days=1)
    second = Transport()
    resumed = run(job(second, clock=lambda: later), as_of=later)
    assert resumed['completedMonths'] == 2 and resumed['confirmedObservations'] == 12
    assert second.calls[0]['startDate'] == '2026-08-01'


@pytest.mark.parametrize('field,bad', [('unit', 'bp'), ('name', 'wrong'), ('region', 'US'),
    ('frequency', 'monthly'), ('seasonal_adjustment', 'wrong'),
    ('source_url', 'https://example.invalid/incorrect'), ('valid_until', NOW)])
def test_same_value_metadata_damage_recovers_with_new_version_and_no_repeat(setup, field, bad):
    factory, cursors, job, _ = setup
    assert run(job(Transport()), request_limit=100)['status'] == 'completed'
    with factory.begin() as session:
        old = session.scalar(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 9, 30)))
        setattr(old, field, bad)
        old_id = old.id
    with factory() as session:
        old = session.get(MacroObservation, old_id)
        old_state = {column.key: getattr(old, column.key) for column in old.__table__.columns}
        old_raws = [dict(row) for row in session.execute(select(SourcePayload.__table__)).mappings()]
    before = cursors.load('usdcny-history-v1')
    assert job(None).status()['completedMonths'] == 11
    assert cursors.load('usdcny-history-v1') == before
    later = NOW + timedelta(days=1)
    fetch = Transport()
    repaired = run(job(fetch, clock=lambda: later), as_of=later, request_limit=100)
    assert repaired['status'] == 'completed' and repaired['rowCount'] == 1, repaired
    assert len(fetch.calls) == 1
    with factory() as session:
        old = session.get(MacroObservation, old_id)
        assert {column.key: getattr(old, column.key) for column in old.__table__.columns} == old_state
        new_raws = [dict(row) for row in session.execute(select(SourcePayload.__table__)).mappings()]
        assert all(row in new_raws for row in old_raws)
        rows = session.scalars(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 9, 30))
            .order_by(MacroObservation.id)).all()
        assert len(rows) == 2 and rows[1].value == rows[0].value
        assert rows[1].revision != rows[0].revision and rows[1].released_at.replace(tzinfo=UTC) == later
    no_http = Transport()
    again = run(job(no_http, clock=lambda: later + timedelta(days=1)),
        as_of=later + timedelta(days=1), request_limit=100)
    assert again['status'] == 'completed' and again['rowCount'] == 0
    assert no_http.calls == []


def test_metadata_repair_core_commit_before_done_remains_idempotent(setup, monkeypatch):
    factory, cursors, job, _ = setup
    assert run(job(Transport()), request_limit=100)['status'] == 'completed'
    with factory.begin() as session:
        old = session.scalar(select(MacroObservation).where(MacroObservation.observed_at == date(2026, 9, 30)))
        old.unit = 'bp'
    original = cursors.save
    def interrupt(key, value):
        if all(entry['status'] == 'done' for entry in value['entries']):
            raise KeyboardInterrupt()
        original(key, value)
    monkeypatch.setattr(cursors, 'save', interrupt)
    later = NOW + timedelta(days=1)
    with pytest.raises(KeyboardInterrupt):
        run(job(Transport(), clock=lambda: later), as_of=later, request_limit=100)
    monkeypatch.setattr(cursors, 'save', original)
    resumed = run(job(Transport(), clock=lambda: later + timedelta(days=1)),
        as_of=later + timedelta(days=1), request_limit=100)
    assert resumed['status'] == 'completed' and resumed['rowCount'] == 0
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 13
