from app.stocks.update import runtime as stock_runtime
from datetime import UTC, datetime
from dataclasses import dataclass

import pytest

from app.config import Settings
from backend.tests.stock_update.test_sync import NOW, task


pytestmark = pytest.mark.usefixtures("isolated_routine_groups")

def module():
    from app.jobs import routine_update
    return routine_update


def settings(*, database_url='postgresql+psycopg://controlled.invalid/test', **changes):
    return Settings(data_backend='database', database_url=database_url, **changes)


def setup_steps(monkeypatch, stocks=None, macro=None):
    m = module()
    calls = []
    def stock_step(s, *, now, dry_run, publish):
        calls.append(('stocks', now, publish))
        return stocks or {'status': 'completed', 'publishReady': True, 'requests': 2,
            'historyGaps': [], 'publication': {'status': 'completed'}}
    def macro_step(s, *, now):
        calls.append(('macro', now, False))
        return macro or {'status': 'partial', 'activeCoverageCompleted': True,
            'requestCount': 3, 'pausedSeries': ['usdcny', 'social-financing']}
    monkeypatch.setattr(m, 'run_stock_step', stock_step)
    monkeypatch.setattr(m, 'run_macro_catchup', macro_step)
    return m, calls


def test_active_only_success_preserves_paused_and_separate_budgets(monkeypatch):
    m, calls = setup_steps(monkeypatch)
    report = m.run_routine_update(settings(), now=NOW, macro_rebuild=lambda s: None)
    assert report['status'] == 'completed'
    assert report['scope'] == 'development-active-only'
    assert report['macro']['status'] == 'partial'
    assert report['macro']['pausedSeries'] == ['usdcny', 'social-financing']
    assert report['httpCounts'] == {'stocks': 2, 'macro': 3, 'total': 5}
    assert [c[:2] for c in calls] == [('stocks', NOW), ('macro', NOW)]
    assert m.routine_exit_code(report) == 0


@pytest.mark.parametrize('status,want', [('disabled', 4), ('partial', 4), ('locked', 5), ('failed', 3)])
def test_incomplete_macro_is_never_hidden_by_stock_success(monkeypatch, status, want):
    m, calls = setup_steps(monkeypatch, macro={'status': status, 'activeCoverageCompleted': False, 'requestCount': 0})
    publications = []
    report = m.run_routine_update(settings(), now=NOW, publish=True, macro_rebuild=lambda s: publications.append(s))
    assert len(calls) == 2 and publications == []
    assert m.routine_exit_code(report) == want


def test_stock_exception_is_sanitized_and_macro_still_runs(monkeypatch):
    m, calls = setup_steps(monkeypatch)
    def fail(*args, **kwargs):
        raise RuntimeError('postgresql://user:private-token@host/database')
    monkeypatch.setattr(m, 'run_stock_step', fail)
    report = m.run_routine_update(settings(), now=NOW, macro_rebuild=lambda s: None)
    assert report['status'] == 'failed' and m.routine_exit_code(report) == 3
    assert report['stocks']['errorCode'] == 'ROUTINE_STOCKS_FAILED'
    assert calls[0][0] == 'macro' and 'private-token' not in str(report)
    assert report['httpCounts'] == {'stocks': None, 'macro': 3, 'total': None}


@dataclass
class Publication:
    status: str
    version: str | None = None


def test_macro_publish_failure_does_not_hide_completed_collection(monkeypatch):
    m, _ = setup_steps(monkeypatch)
    report = m.run_routine_update(settings(), now=NOW, publish=True,
        macro_rebuild=lambda s: Publication('blocked'))
    assert report['macro']['activeCoverageCompleted'] is True
    assert report['macro']['publication']['status'] == 'blocked'
    assert m.routine_exit_code(report) == 4


def test_macro_publish_exception_is_failed_and_sanitized(monkeypatch):
    m, _ = setup_steps(monkeypatch)
    def fail(s):
        raise RuntimeError('private-token')
    report = m.run_routine_update(settings(), now=NOW, publish=True, macro_rebuild=fail)
    assert m.routine_exit_code(report) == 3
    assert 'private-token' not in str(report)


def test_stock_partial_keeps_macro_independent(monkeypatch):
    m, calls = setup_steps(monkeypatch, stocks={'status': 'partial', 'publishReady': False, 'requests': 3})
    report = m.run_routine_update(settings(), now=NOW, macro_rebuild=lambda s: None)
    assert len(calls) == 2 and m.routine_exit_code(report) == 4


def test_configuration_selects_only_stock_group(monkeypatch):
    m, calls = setup_steps(monkeypatch)
    report = m.run_routine_update(settings(routine_macro_enabled=False), now=NOW, macro_rebuild=lambda s: None)
    assert [c[0] for c in calls] == ['stocks']
    assert report['macro']['status'] == 'not_selected'
    assert report['httpCounts'] == {'stocks': 2, 'macro': 0, 'total': 2}
    assert m.routine_exit_code(report) == 0


@pytest.mark.parametrize('changes', [{'routine_stocks_enabled': False, 'routine_macro_enabled': False},
    {'sync_universe': 'all'}, {'sync_universe_file': 'private-token.json'}])
def test_invalid_scope_fails_before_any_source_call(monkeypatch, changes):
    m, calls = setup_steps(monkeypatch)
    with pytest.raises(ValueError):
        m.run_routine_update(settings(**changes), now=NOW, macro_rebuild=lambda s: None)
    assert calls == []


def test_interrupt_is_not_swallowed(monkeypatch):
    m, calls = setup_steps(monkeypatch)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(m, 'run_stock_step', interrupt)
    with pytest.raises(KeyboardInterrupt):
        m.run_routine_update(settings(), now=NOW, macro_rebuild=lambda s: None)
    assert calls == []


def test_real_stock_preflight_does_not_create_http_job_or_cursor(new_pg_factory, monkeypatch):
    from sqlalchemy import select, func
    from app.db.models import JobRun, JobCursor
    m = module()
    job, client, calls = task(new_pg_factory)
    engine = job.factory.kw['bind']
    url = engine.url.render_as_string(hide_password=False)
    monkeypatch.setattr(stock_runtime, 'create_engine_and_session', lambda *a, **k: pytest.fail('只读预检创建了写入引擎'))
    monkeypatch.setattr(stock_runtime, 'TushareClient', lambda *a, **k: (_ for _ in ()).throw(AssertionError('HTTP client forbidden')))
    monkeypatch.setattr(m, 'read_macro_catchup_plan', lambda s, **k: {'readOnly': True, 'activeCount': 18, 'requestCount': 0})
    try:
        report = m.run_routine_update(settings(database_url=url), now=NOW, dry_run=True, publish=True, macro_rebuild=lambda s: None)
        assert report['status'] == 'planned' and report['stocks']['asOf'] is None
        assert report['macro']['activeCount'] == 18 and report['httpCounts']['total'] == 0
        with job.factory() as session:
            assert session.scalar(select(func.count()).select_from(JobRun)) == 0
            assert session.scalar(select(func.count()).select_from(JobCursor)) == 0
        assert calls == []
    finally:
        client.close()


def real_runtime(new_pg_factory, monkeypatch, *, market_ready):
    from app.connectors.tushare.client import TushareClient
    m = module()
    job, client, calls = task(new_pg_factory, market_ready=market_ready)
    engine = job.factory.kw['bind']
    transport = client._client._transport  # external boundary only; DB/cursor/job stay real
    monkeypatch.setattr(stock_runtime, 'TushareClient', lambda token, **kw: TushareClient(token, transport=transport, backoff_seconds=0))
    return m, job, client, calls, engine.url.render_as_string(hide_password=False)


def test_recreated_runtime_after_days_resumes_frozen_units_and_reentry_is_zero_http(new_pg_factory, monkeypatch):
    from datetime import timedelta
    m, job, client, calls, url = real_runtime(new_pg_factory, monkeypatch, market_ready=True)
    try:
        first = m.run_routine_update(settings(database_url=url, routine_macro_enabled=False, tushare_token='private-token',
            stock_update_max_requests_per_run=3), now=NOW, macro_rebuild=lambda s: None)
        assert first['stocks']['status'] == 'partial' and first['httpCounts']['stocks'] == 3
        second = m.run_routine_update(settings(database_url=url, routine_macro_enabled=False, tushare_token='private-token'),
            now=NOW+timedelta(days=6), macro_rebuild=lambda s: None)
        assert second['stocks']['status'] == 'completed'
        assert second['stocks']['asOf'] == '2026-10-13'
        assert second['httpCounts']['stocks'] == 249 and len(calls) == 252
        # Empty classification fixture cannot falsely pass publication readiness.
        assert second['status'] == 'partial' and second['stocks']['classified'] == 0
        third = m.run_routine_update(settings(database_url=url, routine_macro_enabled=False, tushare_token='private-token'),
            now=NOW, macro_rebuild=lambda s: None)
        assert third['httpCounts'] == {'stocks': 0, 'macro': 0, 'total': 0}
        assert len(calls) == 252
    finally:
        client.close()


def test_delayed_supplier_does_not_publish_empty_target(new_pg_factory, monkeypatch):
    m, job, client, calls, url = real_runtime(new_pg_factory, monkeypatch, market_ready=False)
    monkeypatch.setattr(stock_runtime, 'publish_stock_update', lambda *a, **k: (_ for _ in ()).throw(AssertionError('must not publish')))
    try:
        report = m.run_routine_update(settings(database_url=url, routine_macro_enabled=False, tushare_token='private-token'),
            now=NOW, publish=True, macro_rebuild=lambda s: None)
        assert report['stocks']['status'] == 'partial'
        assert report['stocks']['publishReady'] is False
        assert report['httpCounts']['stocks'] == len(calls) == 103
        assert m.routine_exit_code(report) == 4
    finally:
        client.close()


def test_stock_publication_exception_retains_collection_and_http_evidence(new_pg_factory, monkeypatch):
    from datetime import date
    from app.db.models import Classification, ClassificationMember
    m, job, client, calls, url = real_runtime(new_pg_factory, monkeypatch, market_ready=True)
    with job.factory.begin() as session:
        nodes = [Classification(provider='tushare', source='SW2021', classification_id=f'test{level}',
            name=f'层级{level}', level=level, parent_id=f'test{level-1}' if level > 1 else None, data={}) for level in (1, 2, 3)]
        session.add_all(nodes)
        session.flush()
        session.add_all([ClassificationMember(classification_id=nodes[-1].id, ts_code=c,
            effective_from=date(2023, 1, 1), data={}) for c in job.selection.symbols])
    def fail(*args, **kwargs):
        raise RuntimeError('private-token')
    monkeypatch.setattr(stock_runtime, 'publish_stock_update', fail)
    monkeypatch.setattr(m, 'run_macro_catchup', lambda *a, **kw: {'status': 'skipped',
        'activeCoverageCompleted': True, 'requestCount': 0})
    try:
        report = m.run_routine_update(settings(database_url=url, tushare_token='private-token'), now=NOW, publish=True,
            macro_rebuild=lambda s: Publication('completed', 'macro-test-version'))
        assert report['stocks']['status'] == 'completed'
        assert report['stocks']['publishReady'] is True
        assert report['stocks']['publication'] == {'status': 'failed', 'errorCode': 'ROUTINE_STOCK_PUBLICATION_FAILED'}
        assert report['httpCounts'] == {'stocks': 252, 'macro': 0, 'total': 252}
        assert report['macro']['publication']['version'] == 'macro-test-version'
        assert m.routine_exit_code(report) == 3 and 'private-token' not in str(report)
    finally:
        client.close()


def test_missing_stock_token_returns_configuration_exit_and_macro_continues(monkeypatch):
    m = module()
    monkeypatch.setattr(stock_runtime, 'create_engine_and_session', lambda *a: (_ for _ in ()).throw(AssertionError('no writable setup')))
    monkeypatch.setattr(m, 'run_macro_catchup', lambda *a, **kw: {'status': 'skipped',
        'activeCoverageCompleted': True, 'requestCount': 0})
    report = m.run_routine_update(settings(), now=NOW, macro_rebuild=lambda s: None)
    assert m.routine_exit_code(report) == 2
    assert report['macro']['status'] == 'skipped' and report['httpCounts']['total'] == 0


def test_macro_publication_lock_busy_is_exit_five(monkeypatch):
    from app.cli.main import MacroRebuildLockedError
    m, _ = setup_steps(monkeypatch)
    def fail(s):
        raise MacroRebuildLockedError('locked')
    report = m.run_routine_update(settings(), now=NOW, publish=True, macro_rebuild=fail)
    assert m.routine_exit_code(report) == 5


def test_stock_quality_failure_is_blocked_not_runtime_error(monkeypatch):
    m, _ = setup_steps(monkeypatch, stocks={'status': 'completed', 'publishReady': True, 'requests': 0,
        'publication': {'status': 'failed', 'quality': [{'name': 'coverage', 'severity': 'blocking', 'passed': False}]}})
    report = m.run_routine_update(settings(), now=NOW, publish=True, macro_rebuild=lambda s: Publication('completed'))
    assert m.routine_exit_code(report) == 4


def test_preflight_does_not_initialize_missing_schema(pg_url):
    from sqlalchemy import create_engine, inspect
    m = module()
    report = m.run_routine_update(Settings(data_backend='database', database_url=pg_url,
        routine_macro_enabled=False), now=NOW, dry_run=True, macro_rebuild=lambda s: None)
    assert report['status'] != 'planned'
    engine = create_engine(pg_url)
    try:
        assert 'ops' not in inspect(engine).get_schema_names()
    finally:
        engine.dispose()


def test_real_postgres_readonly_preflight_preserves_state_and_rejects_writes(pg_schema_url, monkeypatch):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from app.shared.readonly_database import open_readonly_factory
    from backend.tests.test_cli import _database_state

    before = _database_state(pg_schema_url)
    m = module()
    configured = Settings(data_backend="database", database_url=pg_schema_url, routine_macro_enabled=False)
    report = m.run_routine_update(configured, now=NOW, dry_run=True, macro_rebuild=lambda s: None)
    assert report['status'] == 'planned' and report['httpCounts']['total'] == 0
    from app.cli.main import app
    from typer.testing import CliRunner
    import json
    monkeypatch.setattr(Settings, 'from_env', lambda: configured)
    result = CliRunner().invoke(app, ['update-routine','--dry-run'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['httpCounts']['total'] == 0
    with open_readonly_factory(pg_schema_url) as readonly:
        with readonly() as session:
            with pytest.raises(DBAPIError) as error:
                session.execute(text("INSERT INTO ops.job_cursor(job_name,cursor_key,cursor_value) VALUES ('illegal','test','{}')"))
            assert error.value.orig.sqlstate == "25006"
    assert _database_state(pg_schema_url) == before


def test_real_stock_publication_lock_uses_typed_busy_contract(pg_factory):
    from app.jobs.locks import ProcessLocalJobLock
    from app.jobs.run_errors import JobLockedError
    from app.stocks.update.publication import publish_stock_update

    from app.stocks.universes import UniverseResolver
    from datetime import date
    lock = ProcessLocalJobLock()
    key = 'sync-stock-data:representative-50'
    assert lock.try_acquire(key)
    try:
        with pytest.raises(JobLockedError):
            publish_stock_update(pg_factory, UniverseResolver().resolve('representative-50'),
                date(2026,10,13), lock, collection={'status':'completed','publishReady':True,'identity':'test'})
    finally:
        lock.release(key)
