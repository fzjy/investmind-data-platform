from sqlalchemy import create_engine, text


def test_databases_are_independent(pg_database):
    first = create_engine(pg_database())
    second = create_engine(pg_database())
    try:
        with first.begin() as connection:
            assert connection.scalar(text('select 1')) == 1
            connection.execute(text('create table isolation_probe (id integer)'))
        with second.connect() as connection:
            assert connection.scalar(text("select to_regclass('public.isolation_probe')")) is None
    finally:
        first.dispose()
        second.dispose()


def test_child_shares_only_case_database(pg_url, monkeypatch):
    import json
    import pytest
    from backend.tests.support.postgres import run_child
    monkeypatch.setenv('PG_TEST_SENTINEL', 'harmless-parent-value')
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(text('create table child_probe (id integer)'))
            connection.execute(text('insert into child_probe values (42)'))
        result = run_child(pg_url, 'backend.tests.isolation_probe', 'postgres')
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {'postgres': 1, 'shared': 42, 'dotenvDisabled': True, 'pgEnvironment': []}
        rejected = run_child(pg_url, 'backend.tests.isolation_probe', 'database')
        assert rejected.returncode == 97
        assert 'violations=1' in rejected.stderr
        with pytest.raises(AssertionError):
            run_child('postgresql+psycopg://canary:canary@127.0.0.1:1/business',
                      'backend.tests.isolation_probe', 'postgres')
    finally:
        engine.dispose()


def test_public_entry_rejects_child_url_before_database_access():
    import json
    from pathlib import Path
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, '-I', str(root / 'backend/tests/run_isolated.py'),
         '--registered-child', 'backend.tests.isolation_probe', 'postgres'],
        input=json.dumps({'url': 'postgresql+psycopg://canary:canary@127.0.0.1:1/business'}),
        text=True, capture_output=True, timeout=15, cwd=root,
    )
    assert result.returncode != 0
    assert result.stderr.strip() == 'Only pytest or backend.tests modules are allowed'
