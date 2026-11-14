"""估值补采专项：仅内存数据库与合成HTTP原件。"""
import base64
from copy import deepcopy
from datetime import date, datetime, timezone, timedelta
from hashlib import sha256
import importlib.util
import json

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from app.db.models import JobCursor, IngestionBatch, SourcePayload, PublishedSnapshot
from app.connectors.tushare.contracts import TushareResponse

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


def test_public_supply_interface_exists():
    assert module().load_valuation_rows


def module():
    assert importlib.util.find_spec('app.stocks.valuation_supply') is not None, '缺少独立估值供给实现'
    from app.stocks import valuation_supply
    return valuation_supply


@pytest.fixture
def factory(monkeypatch, tmp_path):
    m = module()
    path = tmp_path / 'financial.json'
    path.write_text(json.dumps({'valuationSupply': dict(enabled=True, maxHttpPerRun=20, timeoutSeconds=20, historyYears=5, revisionDays=7)}))
    monkeypatch.setattr(m, 'CONFIG_FILE', path)
    engine = create_engine('sqlite://', execution_options={'schema_translate_map': {'ops': None, 'raw': None, 'mart': None}})
    for model in (JobCursor, IngestionBatch, SourcePayload, PublishedSnapshot):
        model.__table__.create(engine)
    f = sessionmaker(engine, expire_on_commit=False)
    with f.begin() as s:
        s.add(PublishedSnapshot(product='screener', version='original', as_of=NOW, payload={'stocks': [{'symbol': '600519.SH'}]}))
    yield f
    engine.dispose()


class Client:
    def __init__(self, *, pages=False, fail=False, rows=None):
        self.calls = []
        self.pages, self.fail, self.rows = pages, fail, rows

    def set_response_observer(self, observer):
        self.observer = observer

    def call(self, api, params, fields):
        self.calls.append((api, deepcopy(params), tuple(fields)))
        if self.fail:
            raise TimeoutError('不得重试')
        offset = params.get('offset', 0)
        rows = self.rows if self.rows is not None else [[params['ts_code'], '20261105' if offset else '20261106', None, 2]]
        data = dict(fields=list(fields), items=rows, has_more=self.pages and offset == 0)
        body = json.dumps(dict(code=0, data=data)).encode()
        self.observer(dict(statusCode=200, credentialsRedacted=False, bodyBase64=base64.b64encode(body).decode(), bodySha256=sha256(body).hexdigest(), byteLength=len(body)))
        return TushareResponse.model_validate(data)


def test_preview_and_load_before_capture_are_readonly(factory):
    m = module()
    assert m.capture(factory, now=NOW)['status'] == 'planned'
    assert m.load_valuation_rows(factory, ['600519.SH'], NOW.date()) == {}
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(JobCursor)) == 0


def test_bounded_scope_resume_cross_day_and_superset_load(factory):
    m = module()
    with factory.begin() as s:
        p = s.get(PublishedSnapshot, 'screener')
        p.payload = {'stocks': [{'symbol': f'{i:06}.SZ'} for i in range(50)]}
    c = Client()
    for day, count, status in [(0, 20, 'pending'), (1, 20, 'pending'), (2, 10, 'completed')]:
        r = m.capture(factory, c, execute=True, now=NOW + timedelta(days=day))
        assert (r['requestCount'], r['status']) == (count, status)
    assert len(c.calls) == 50
    assert c.calls[0] == ('daily_basic', {'ts_code': '000000.SZ', 'start_date': '20211106', 'end_date': '20261106'}, ('ts_code', 'trade_date', 'pe', 'pb'))
    assert m.capture(factory, c, execute=True, now=NOW + timedelta(days=3))['requestCount'] == 0
    symbols = [f'{i:06}.SZ' for i in range(51)]
    rows = m.load_valuation_rows(factory, symbols, date(2026, 11, 9))
    assert len(rows) == 50
    assert rows['000000.SZ'][0].trade_date == date(2026, 11, 6)
    assert rows['000000.SZ'][0].pe is None
    assert m.load_valuation_rows(factory, symbols[:1], NOW.date()) == {}
    with factory() as s:
        history = m.risk_valuation_history(s, symbols[:1], date(2026, 11, 9))
        assert list(history) == symbols[:1]
        assert history[symbols[0]]["start"] == "2021-11-06"
        assert history[symbols[0]]["rows"]["2026-11-06"] == [None, 2]


def test_paging_resumes_offsets_and_counts_every_http(factory):
    m = module(); c = Client(pages=True)
    r = m.capture(factory, c, execute=True, now=NOW, max_http=1)
    assert (r['requestCount'], r['status']) == (1, 'pending')
    assert m.load_valuation_rows(factory, ['600519.SH'], NOW.date()) == {}
    r = m.capture(factory, c, execute=True, now=NOW + timedelta(days=1), max_http=1)
    assert (r['requestCount'], r['status']) == (1, 'completed')
    assert c.calls[1][1]['offset'] == 1
    assert len(m.load_valuation_rows(factory, ['600519.SH'], NOW.date())['600519.SH']) == 2


def test_failed_reservation_cannot_escape_by_day_version_or_config(factory):
    m = module(); c = Client(fail=True)
    r = m.capture(factory, c, execute=True, now=NOW)
    assert (r['status'], r['requestCount']) == ('blocked', 1)
    with factory.begin() as s:
        s.get(PublishedSnapshot, 'screener').version = 'new'
    r = m.capture(factory, c, execute=True, now=NOW + timedelta(days=30))
    assert (r['status'], r['requestCount']) == ('blocked', 0)
    with factory() as s:
        assert sum(s.scalars(select(IngestionBatch.request_count))) == 1


@pytest.mark.parametrize('damage', ['bytes', 'request', 'batch', 'hash'])
def test_bad_saved_evidence_blocks_load_and_reentry(factory, damage):
    m = module(); c = Client()
    assert m.capture(factory, c, execute=True, now=NOW)['status'] == 'completed'
    with factory.begin() as s:
        raw = s.scalar(select(SourcePayload))
        if damage == 'bytes':
            raw.payload = raw.payload | {'_http': raw.payload['_http'] | {'bodyBase64': 'bad'}}
            from app.ingestion.repositories import content_hash
            raw.content_hash = content_hash(raw.payload)
            unit = s.scalar(select(JobCursor).where(JobCursor.cursor_key.not_like('contract:%')))
            unit.cursor_value = unit.cursor_value | {'responseContentHash': raw.content_hash}
        elif damage == 'request': raw.request_hash = 'wrong'
        elif damage == 'batch': s.get(IngestionBatch, raw.batch_id).request_count = 0
        else: raw.content_hash = 'wrong'
    assert m.capture(factory, c, execute=True, now=NOW)['status'] == 'blocked'
    with pytest.raises(ValueError): m.load_valuation_rows(factory, ['600519.SH'], NOW.date())
    assert len(c.calls) == 1


def test_partial_frozen_scope_change_blocks_before_any_extra_http(factory):
    m=module();c=Client(pages=True)
    assert m.capture(factory,c,execute=True,now=NOW,max_http=1)['status']=='pending'
    with factory.begin() as s:
        s.get(PublishedSnapshot,'screener').payload={'stocks':[{'symbol':'600519.SH'},{'symbol':'000001.SZ'}]}
    report=m.capture(factory,c,execute=True,now=NOW)
    assert report['status']=='blocked' and report['requestCount']==0
    assert len(c.calls)==1


def test_reuse_ignores_parent_version_and_expires_after_completion(factory):
    m = module(); c = Client()
    m.capture(factory, c, execute=True, now=NOW)
    with factory.begin() as s:
        p = s.get(PublishedSnapshot, 'screener'); p.version = 'price-changed'; p.as_of = NOW + timedelta(days=3)
    assert m.capture(factory, c, execute=True, now=NOW + timedelta(days=3))['requestCount'] == 0
    assert m.capture(factory, c, execute=True, now=NOW + timedelta(days=8))['requestCount'] == 1


def test_conflicts_are_null_missing_never_zero(factory):
    m = module(); c = Client(rows=[['600519.SH', '20261106', 10, None], ['600519.SH', '20261106', 11, None]])
    assert m.capture(factory, c, execute=True, now=NOW)['status'] == 'completed'
    row = m.load_valuation_rows(factory, ['600519.SH'], NOW.date())['600519.SH'][0]
    assert row.pe is None and row.pb is None


@pytest.mark.parametrize('row', [['600519.SZ', '20261106', 1, 2], ['600519.SH', '20261107', 1, 2], ['600519.SH', '20201106', 1, 2]])
def test_wrong_symbol_future_and_outside_window_are_blocked(factory, row):
    r = module().capture(factory, Client(rows=[row]), execute=True, now=NOW)
    assert (r['status'], r['requestCount']) == ('blocked', 1)


def test_fixed_lock_is_released_and_contention_zero_http(factory):
    m = module()
    from app.jobs.locks import job_lock_for_engine
    lock = job_lock_for_engine(factory.kw['bind'])
    assert lock.try_acquire(m.JOB + ':targets')
    try:
        assert m.capture(factory, Client(), execute=True, now=NOW)['requestCount'] == 0
    finally: lock.release(m.JOB + ':targets')
    assert m.capture(factory, Client(), execute=True, now=NOW)['status'] == 'completed'


def test_cli_defaults_readonly_and_explicit_publish(factory, monkeypatch):
    import typer
    from typer.testing import CliRunner
    from app.cli import valuation_supply as cli
    calls = []
    monkeypatch.setattr(cli.Settings, 'from_env', lambda: object())
    def run(settings, **kwargs):
        calls.append(kwargs)
        return dict(status='planned' if kwargs['dry_run'] else 'completed', requestCount=0)
    monkeypatch.setattr(cli, 'run_valuation_step', run)
    app = typer.Typer()
    cli.register_valuation_supply(app)
    for args, readonly, publish in [([], True, False), (['--execute'], False, False), (['--publish'], False, True), (['--dry-run', '--publish'], True, True)]:
        assert CliRunner().invoke(app, args).exit_code == 0
        assert calls[-1]['dry_run'] == readonly and calls[-1]['publish_requested'] == publish


@pytest.mark.parametrize('key,value', [('maxHttpPerRun', 21), ('maxHttpPerRun', True), ('timeoutSeconds', 0), ('revisionDays', 0), ('historyYears', 100), ('enabled', 1)])
def test_invalid_config_cannot_spend_http(factory, key, value):
    m = module()
    config = json.loads(m.CONFIG_FILE.read_text())
    config['valuationSupply'][key] = value
    m.CONFIG_FILE.write_text(json.dumps(config))
    assert m.capture(factory, Client(), execute=True, now=NOW)['requestCount'] == 0
    assert m.capture(factory, Client(), execute=True, now=NOW)['status'] == 'blocked'


def test_dry_runtime_opens_only_readonly_and_disabled_opens_nothing(factory, monkeypatch):
    from contextlib import contextmanager
    import app.shared.readonly_database as readonly
    import app.db.session as db
    m = module()
    @contextmanager
    def opened(url):
        assert url == 'readonly-test'
        yield factory
    def forbidden(*args, **kwargs):
        pytest.fail('只读运行不能创建写入连接或HTTP客户端')
    monkeypatch.setattr(readonly, 'open_readonly_factory', opened)
    monkeypatch.setattr(db, 'create_engine_and_session', forbidden)
    from app.connectors.tushare import client
    monkeypatch.setattr(client, 'TushareClient', forbidden)
    from types import SimpleNamespace
    settings = SimpleNamespace(database_url='readonly-test')
    r = m.run_valuation_step(settings, now=NOW, dry_run=True, publish_requested=True)
    assert (r['status'], r['requestCount']) == ('planned', 0)
    m.CONFIG_FILE.write_text(json.dumps({'valuationSupply': {'enabled': False}}))
    assert m.run_valuation_step(settings, now=NOW, dry_run=False)['status'] == 'not_selected'


def test_reserved_without_response_stays_blocked_across_days(factory):
    m = module(); m.capture(factory, Client(), execute=True, now=NOW)
    with factory.begin() as s:
        unit = s.scalar(select(JobCursor).where(JobCursor.job_name == m.JOB, JobCursor.cursor_key.not_like('contract:%'), JobCursor.cursor_key.not_like('completed:%')))
        unit.cursor_value = unit.cursor_value | {'status': 'reserved'}
    r = m.capture(factory, Client(), execute=True, now=NOW + timedelta(days=10))
    assert (r['status'], r['requestCount']) == ('blocked', 0)


@pytest.mark.parametrize('rows', [[], [['600519.SH', '20261106', 1, 2]]])
def test_has_more_without_progress_is_blocked(factory, rows):
    class Repeated(Client):
        def call(self, api, params, fields):
            return super().call(api, params | {'offset': 0}, fields)
    c = Repeated(pages=True, rows=rows)
    result = module().capture(factory, c, execute=True, now=NOW)
    assert result['status'] == 'blocked'
    assert result['requestCount'] == (2 if rows else 1)


def test_incomplete_refresh_can_read_prior_complete_but_corruption_never_falls_back(factory):
    m = module(); m.capture(factory, Client(), execute=True, now=NOW)
    m.capture(factory, Client(pages=True), execute=True, now=NOW + timedelta(days=8), max_http=1)
    assert len(m.load_valuation_rows(factory, ['600519.SH'], NOW.date())['600519.SH']) == 1
    with factory.begin() as s:
        raw = s.scalar(select(SourcePayload).order_by(SourcePayload.id.desc()))
        raw.request_hash = 'bad-newest'
    with pytest.raises(ValueError): m.load_valuation_rows(factory, ['600519.SH'], NOW.date())


def test_installed_wheel_without_config_does_not_select_or_open_database(monkeypatch, tmp_path):
    m = module()
    monkeypatch.setattr(m, 'CONFIG_FILE', tmp_path / 'missing-financial.json')
    assert m.valuation_config()['enabled'] is False
    from types import SimpleNamespace
    # 无数据库设置也应直接跳过，证明未开启连接或HTTP。
    assert m.run_valuation_step(SimpleNamespace(), now=NOW, dry_run=False) == dict(status='not_selected', requestCount=0)


def test_real_completed_step_exposes_publish_ready_to_parent(factory,monkeypatch):
    from types import SimpleNamespace
    m=module();assert m.capture(factory,Client(),execute=True,now=NOW)['status']=='completed'
    monkeypatch.setattr('app.db.session.create_engine_and_session',lambda url:(SimpleNamespace(dispose=lambda:None),factory))
    monkeypatch.setattr('app.stocks.valuation_calendar.capture_calendar',lambda *args,**kwargs:dict(status='completed',requestCount=0,publishReady=True))
    report=m.run_valuation_step(SimpleNamespace(database_url='isolated'),now=NOW,dry_run=False)
    assert report['publishReady'] is True and report['requestCount']==0
