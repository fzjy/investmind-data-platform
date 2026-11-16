from importlib import import_module

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.main as main_module
from app.config import Settings
from app.shared.errors import DataServiceError


def test_production_requires_database_url_before_engine_creation(monkeypatch):
    monkeypatch.setattr(main_module, 'settings', Settings(_env_file=None, database_url=None, database_read_url=None))
    monkeypatch.setattr('app.db.session.create_engine', lambda *a, **k: pytest.fail('must reject before engine creation'))
    with pytest.raises(ValueError, match='DANG_DATABASE'):
        main_module.create_app()


@pytest.mark.parametrize('domain', ['market', 'macro', 'portfolio', 'stocks', 'search'])
def test_routes_require_explicit_dependencies(domain):
    routes = import_module(f'app.{domain}.routes')
    assert not hasattr(routes, 'router')
    with pytest.raises((ValueError, TypeError)):
        routes.create_router()


@pytest.mark.parametrize('path', [
    '/api/data/market-overview', '/api/data/market-explorer',
    '/api/data/macro-transmission', '/api/data/macro-insights',
    '/api/data/portfolio', '/api/data/stocks/600519.SH',
])
def test_database_outage_returns_503_without_fixture_fallback(monkeypatch, path):
    from sqlalchemy.engine import Engine

    monkeypatch.setattr(main_module, 'settings', Settings(_env_file=None, database_url='postgresql+psycopg://controlled.invalid/test'))
    def unavailable(*args, **kwargs):
        raise OperationalError('private query', {}, Exception('private credentials'))
    monkeypatch.setattr(Engine, 'connect', unavailable)
    client = TestClient(main_module.create_app(), raise_server_exceptions=False)
    response = client.get(path)
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'DATA_SOURCE_UNAVAILABLE'
    assert 'private' not in response.text
    health = client.get('/health')
    assert health.status_code == 503
    assert health.json() == {'status': 'degraded', 'database': 'unavailable'}


def test_application_construction_does_not_connect(monkeypatch):
    from sqlalchemy.engine import Engine

    monkeypatch.setattr(Engine, 'connect', lambda *a, **k: pytest.fail('construction must not connect'))
    assert main_module.create_app().title == 'InvestMind Data API'


@pytest.mark.parametrize('error', [
    OperationalError('private query', {}, Exception('private credentials')),
    DataServiceError('DATA_NOT_READY', '尚未发布'),
    RuntimeError('private traceback'),
])
def test_fixture_factory_preserves_production_error_contract(monkeypatch, error):
    from backend.tests.support.application import create_app as create_test_app

    responses = []
    for factory in (main_module.create_app, create_test_app):
        application = factory()
        @application.get('/contract-error')
        def fail():
            raise error
        response = TestClient(application, raise_server_exceptions=False).get('/contract-error')
        body = response.json()
        assert body['meta'].pop('requestId')
        assert 'private' not in response.text
        responses.append((response.status_code, body))
    assert responses[0] == responses[1]
