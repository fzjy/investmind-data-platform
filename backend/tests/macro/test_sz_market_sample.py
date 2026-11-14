from copy import deepcopy
from datetime import timedelta
from importlib import import_module
import json

import httpx
import pytest
from sqlalchemy import select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import IngestionBatch, SourcePayload
from backend.tests.macro.test_extension_store import database, count
from backend.tests.macro.test_exchange_summary_plan import build
from backend.tests.macro.test_exchange_summary_store import complete, store, NOW
from backend.tests.macro.test_exchange_summary_client import TOKEN, response


def module():
    return import_module('app.macro.sz_market_sample')


def parent(factory):
    plan = build(group='aShareFloatMv')
    s = store(factory)
    complete(s, plan, plan['documents'][0])
    doc = plan['documents'][1]
    key = complete(s, plan, doc, NOW + timedelta(seconds=3), {'fields': doc['query']['fields'], 'items': []})
    return key


def policy(key):
    return {'enabled': True, 'parentRunId': key, 'providerUrl': 'https://api.tushare.pro',
        'documentationUrl': 'https://tushare.pro/document/2?doc_id=268', 'apiName': 'sz_daily_info',
        'fields': ['trade_date', 'ts_code', 'count', 'float_mv'], 'unitStatus': 'unverified',
        'useScope': 'personal-local-noncommercial',
        'limits': {'maxTotalHttp': 1, 'maxHttpPerRun': 1, 'requestsPerMinute': 1,
            'requestTimeoutSeconds': 20, 'maxRetries': 0, 'maxResponseBytes': 1048576, 'maxResponseRows': 64}}


def sample(date='20261022'):
    return {'fields': ['trade_date', 'ts_code', 'count', 'float_mv'],
        'items': [[date, '主板A股', 1493, '21950067551388'], [date, '创业板A股', 1400, '14302979332510']]}


def execute(factory, value, handler, **kwargs):
    runtime = import_module('app.macro.jobs.sz_market_sample_runtime')
    return runtime.execute_sz_market_sample(factory, module().SzMarketSamplePolicy.model_validate(value), TOKEN,
        transport=httpx.MockTransport(handler), clock=kwargs.pop('clock', lambda: NOW + timedelta(minutes=1)),
        policy_loader=kwargs.pop('policy_loader', lambda: value), **kwargs)


def test_parent_bound_plan_and_old_configuration_compatibility(database):
    _, factory = database
    key = parent(factory)
    m = module()
    plan = m.build_sample_plan(factory, m.SzMarketSamplePolicy.model_validate(policy(key)), NOW + timedelta(minutes=1))
    assert plan['targetDate'] == '2026-10-22'
    assert plan['query'] == {'api': 'sz_daily_info', 'params': {'trade_date': '20261022'}, 'fields': policy(key)['fields']}
    assert plan['parent']['runId'] == key and plan['coreEligible'] is False
    from app.macro.extensions.config import ExtensionConfig, load_extension_config
    config = load_extension_config().model_dump()
    groups = deepcopy(config['groups'])
    config['contractSamples'] = {'szMarket': policy(key)}
    checked = ExtensionConfig.model_validate(config)
    assert checked.model_dump()['groups'] == groups
    assert checked.contractSamples['szMarket'].unitStatus == 'unverified'
    del config['contractSamples']
    assert ExtensionConfig.model_validate(config).contractSamples == {}


@pytest.mark.parametrize('field,bad', [('providerUrl', 'http://api.tushare.pro'), ('apiName', 'daily_basic'),
    ('unitStatus', 'verified'), ('fields', ['trade_date', 'token']), ('parentRunId', 'not-uuid'), ('enabled', 1),
    ('extra', True), ('useScope', 'commercial')])
def test_closed_policy_rejects_unsafe_overrides(field, bad):
    value = policy('791bea1e-7eaa-5a72-aefd-848d8edcfb27')
    value[field] = bad
    with pytest.raises(ValueError):
        module().SzMarketSamplePolicy.model_validate(value)


@pytest.mark.parametrize('field,bad', [('maxTotalHttp', 2), ('maxHttpPerRun', 2), ('maxRetries', 1),
    ('maxResponseRows', 65), ('maxResponseBytes', 1048577), ('requestTimeoutSeconds', 21)])
def test_pilot_cannot_expand_budget(field, bad):
    value = policy('791bea1e-7eaa-5a72-aefd-848d8edcfb27')
    value['limits'][field] = bad
    with pytest.raises(ValueError):
        module().SzMarketSamplePolicy.model_validate(value)


def test_single_lifetime_reservation_precedes_http_and_no_core_or_second_request(database):
    _, factory = database
    value = policy(parent(factory))
    before = {model: count(factory, model) for model in (JobCursor, JobRun, IngestionBatch, SourcePayload, MacroObservation)}
    calls = []
    def handler(request):
        calls.append(request)
        assert count(factory, JobRun) == before[JobRun] + 1
        body = json.loads(request.content)
        assert body == {'api_name': 'sz_daily_info', 'token': TOKEN,
            'params': {'trade_date': '20261022'}, 'fields': 'trade_date,ts_code,count,float_mv'}
        assert str(request.url) == 'https://api.tushare.pro'
        return response(value={'code': 0, 'msg': 'discard', 'data': sample()})
    result = execute(factory, value, handler)
    assert result['status'] == 'captured' and result['supplierHttp'] == 1
    assert result['summary']['aShareRowsPresent'] and not result['summary']['unitVerified']
    assert result['summary']['coreEligible'] is False
    assert count(factory, MacroObservation) == before[MacroObservation]
    for model in (JobCursor, JobRun, IngestionBatch, SourcePayload):
        assert count(factory, model) == before[model] + 1
    again = execute(factory, value, handler, clock=lambda: NOW + timedelta(days=5))
    assert again['readOnly'] and again['supplierHttp'] == 0 and len(calls) == 1
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize('kind', ['permission', 'echo', 'redirect', 'large', 'timeout', 'bad-json', 'has-more'])
def test_failures_are_sanitized_consumed_and_never_retried(database, kind):
    _, factory = database
    value = policy(parent(factory))
    calls = []
    def handler(request):
        calls.append(request)
        if kind == 'timeout':
            raise httpx.ReadTimeout(TOKEN)
        if kind == 'redirect':
            return response(302, headers={'location': 'https://example.org'})
        if kind == 'large':
            return response(content=b'x' * 1048577)
        if kind == 'bad-json':
            return response(content=b'{bad')
        if kind == 'permission':
            return response(value={'code': 2002, 'msg': TOKEN})
        if kind == 'echo':
            return response(value={'code': 0, 'msg': TOKEN, 'data': sample()})
        return response(value={'code': 0, 'data': {**sample(), 'has_more': True}})
    result = execute(factory, value, handler)
    assert result['status'] == 'blocked' and result['supplierHttp'] == 1
    assert TOKEN not in json.dumps(result) and result['errorCode'].startswith('EXTENSION_')
    assert execute(factory, value, handler)['supplierHttp'] == 0 and len(calls) == 1


def test_unknown_reservation_and_changed_parent_cannot_create_new_allowance(database):
    _, factory = database
    value = policy(parent(factory))
    m = module()
    plan = m.build_sample_plan(factory, m.SzMarketSamplePolicy.model_validate(value), NOW + timedelta(minutes=1))
    s = import_module('app.macro.sz_market_sample_store').SzMarketSampleStore(factory)
    s.reserve(plan, NOW + timedelta(minutes=1))
    def forbidden(_):
        pytest.fail('unknown reservation cannot send again')
    assert execute(factory, value, forbidden)['supplierHttp'] == 0
    changed = deepcopy(value)
    changed['limits']['requestTimeoutSeconds'] = 19
    assert execute(factory, changed, forbidden)['supplierHttp'] == 0
    assert count(factory, JobCursor) == 2


@pytest.mark.parametrize('change', ['empty', 'wrong-date', 'duplicate', 'null', 'zero', 'unknown-category'])
def test_structural_samples_remain_unverified_even_when_numbers_present(database, change):
    _, factory = database
    value = policy(parent(factory))
    data = sample()
    if change == 'empty': data['items'] = []
    elif change == 'wrong-date': data['items'][0][0] = '20261023'
    elif change == 'duplicate': data['items'].append(data['items'][0])
    elif change == 'null': data['items'][0][3] = None
    elif change == 'zero': data['items'][0][3] = 0
    else: data['items'].append(['20261022', 'unrecognized', 1, '100'])
    result = execute(factory, value, lambda _: response(value={'code': 0, 'data': data}))
    assert result['status'] == 'captured' and result['summary']['coreEligible'] is False
    assert result['summary']['unitVerified'] is False
    with factory() as session:
        raw = session.scalars(select(SourcePayload).where(SourcePayload.api_name == 'sz_daily_info')).one()
        assert raw.payload['businessData']['items'] == [[str(v) if isinstance(v, int) and i == 3 else v
            for i, v in enumerate(row)] for row in data['items']]


def test_disabled_and_config_changed_before_send_never_send(database):
    _, factory = database
    value = policy(parent(factory))
    calls = []
    handler = lambda request: calls.append(request)
    off = {**value, 'enabled': False}
    assert execute(factory, off, handler)['status'] == 'disabled'
    result = execute(factory, value, handler, policy_loader=lambda: off)
    assert result['supplierHttp'] == 0 and calls == []


def test_dry_run_does_not_reserve_or_require_token(database):
    _, factory = database
    value = policy(parent(factory))
    n = count(factory, JobRun)
    result = execute(factory, value, lambda _: pytest.fail('dry run HTTP'), dry_run=True)
    assert result['readOnly'] and result['supplierHttp'] == 0 and result['status'] == 'preview'
    assert count(factory, JobRun) == n


@pytest.mark.parametrize('field,bad', [('maxTotalHttp', True), ('maxTotalHttp', 1.0), ('maxRetries', False)])
def test_budget_literals_require_exact_integer_types(field, bad):
    value = policy('791bea1e-7eaa-5a72-aefd-848d8edcfb27')
    value['limits'][field] = bad
    with pytest.raises(ValueError):
        module().SzMarketSamplePolicy.model_validate(value)


def test_raw_persistence_failure_keeps_http_upper_bound_and_unknown_slot(database, monkeypatch):
    _, factory = database
    value = policy(parent(factory))
    cls = import_module('app.macro.sz_market_sample_store').SzMarketSampleStore
    def fail(*args, **kwargs):
        raise RuntimeError(TOKEN)
    monkeypatch.setattr(cls, 'finish', fail)
    result = execute(factory, value, lambda _: response(value={'code': 0, 'data': sample()}))
    assert result['status'] == 'blocked' and result['supplierHttp'] == 1
    assert TOKEN not in json.dumps(result)
    again = execute(factory, value, lambda _: pytest.fail('unknown slot cannot retry'))
    assert again['supplierHttp'] == 0


def test_nonempty_or_wrong_parent_and_tampered_raw_do_not_send(database):
    _, factory = database
    key = parent(factory)
    with factory() as session:
        wrong = session.scalar(select(JobRun).where(JobRun.id != key))
    value = policy(wrong.id)
    with pytest.raises(ValueError):
        execute(factory, value, lambda _: pytest.fail('wrong parent'))
    value = policy(key)
    execute(factory, value, lambda _: response(value={'code': 0, 'data': sample()}))
    from app.macro.extensions.plan import digest
    with factory.begin() as session:
        raw = session.scalars(select(SourcePayload).where(SourcePayload.api_name == 'sz_daily_info')).one()
        raw.payload = {**raw.payload, 'unitVerified': True}
        raw.content_hash = digest(raw.payload)
    with pytest.raises(ValueError):
        execute(factory, value, lambda _: pytest.fail('tampered raw cannot send'))


def test_test_launcher_disables_sample_without_altering_frozen_parent(tmp_path, monkeypatch):
    import app.macro.extensions.config as config_module
    from backend.tests.run_isolated import isolate_extension_config
    value = config_module.load_extension_config().model_dump()
    key = '791bea1e-7eaa-5a72-aefd-848d8edcfb27'
    value['contractSamples'] = {'szMarket': policy(key)}
    configured = config_module.ExtensionConfig.model_validate(value)
    monkeypatch.setattr(config_module, 'load_extension_config', lambda: configured)
    monkeypatch.setattr(config_module, 'DEFAULT_EXTENSION_CONFIG_FILE', config_module.DEFAULT_EXTENSION_CONFIG_FILE)
    isolate_extension_config(tmp_path)
    result = json.loads((tmp_path / 'macro-extensions.json').read_text())
    assert result['contractSamples']['szMarket']['enabled'] is False
    assert result['contractSamples']['szMarket']['parentRunId'] == key


def test_real_new_parent_and_new_day_do_not_reset_project_pilot(database):
    _, factory = database
    value = policy(parent(factory))
    execute(factory, value, lambda _: response(value={'code': 0, 'data': sample()}))
    s = store(factory)
    next_plan = build('2026-10-26T19:00:00+08:00', group='aShareFloatMv')
    later = NOW + timedelta(days=1)
    complete(s, next_plan, next_plan['documents'][0], later)
    doc = next_plan['documents'][1]
    key = complete(s, next_plan, doc, later + timedelta(seconds=3), {'fields': doc['query']['fields'], 'items': []})
    result = execute(factory, policy(key), lambda _: pytest.fail('new parent cannot expand lifetime budget'),
        clock=lambda: later + timedelta(minutes=1))
    assert result['status'] == 'blocked' and result['supplierHttp'] == 0


def test_wrapper_dry_run_uses_existing_readonly_database(database, monkeypatch):
    file, factory = database
    value = policy(parent(factory))
    from app.config import Settings
    from app.macro.extensions.config import ExtensionConfig, load_extension_config
    runtime = import_module('app.macro.jobs.sz_market_sample_runtime')
    data = load_extension_config().model_dump()
    data['contractSamples'] = {'szMarket': value}
    config = ExtensionConfig.model_validate(data)
    monkeypatch.setattr(runtime, 'load_extension_config', lambda: config)
    settings = Settings(data_backend='database', database_url='sqlite:///' + file.as_posix(),
        database_read_url='sqlite:///' + file.as_posix())
    n = count(factory, JobRun)
    preview = runtime.run_sz_market_sample(settings, dry_run=True)
    assert preview['status'] == 'preview' and preview['supplierHttp'] == 0
    assert count(factory, JobRun) == n
    assert runtime.run_sz_market_sample(settings)['errorCode'] == 'EXTENSION_POSTGRES_REQUIRED'


def test_cli_has_no_force_date_or_parent_override_and_sanitizes_failure(monkeypatch):
    import typer
    from typer.testing import CliRunner
    from app.cli.macro_extensions import register_macro_extension_commands
    runtime = import_module('app.macro.jobs.sz_market_sample_runtime')
    calls = []
    def run(settings, *, dry_run=False):
        calls.append(dry_run)
        if not dry_run:
            raise ValueError(TOKEN)
        return {'status': 'preview', 'readOnly': True, 'supplierHttp': 0}
    monkeypatch.setattr(runtime, 'run_sz_market_sample', run)
    app = typer.Typer()
    register_macro_extension_commands(app)
    runner = CliRunner()
    good = runner.invoke(app, ['sample-sz-market-contract', '--dry-run'])
    assert good.exit_code == 0 and json.loads(good.stdout)['supplierHttp'] == 0
    bad = runner.invoke(app, ['sample-sz-market-contract'])
    assert bad.exit_code == 2 and TOKEN not in bad.stdout
    assert json.loads(bad.stdout)['errorCode'] == 'SZ_SAMPLE_CONFIGURATION_INVALID'
    for option in ['--force', '--date', '--parent-run-id', '--source']:
        assert runner.invoke(app, ['sample-sz-market-contract', option, 'x']).exit_code == 2
    assert calls == [True, False]
