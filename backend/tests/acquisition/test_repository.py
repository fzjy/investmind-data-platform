from datetime import UTC, datetime, timedelta
from dataclasses import replace

import pytest

from app.acquisition.contracts import Attempt, Evidence, StateKey
from app.acquisition.repository import StateRepository

NOW = datetime(2026, 11, 10, tzinfo=UTC)
KEY = StateKey('stock-dividend', '', 'security', '600519.SH')


def attempt(i, scope='window1'):
    return Attempt(KEY, str(i), NOW+timedelta(seconds=i), {'window':scope}, 'manual')


def finish(repo, item, status):
    assert repo.begin(item)
    success = item.started_at if status == 'success' else None
    return repo.finish(item, Evidence(status,item.started_at,success,
                                      'REQUEST_FAILED' if status == 'failed' else None,
                                      '', item.scope, ()))


def test_fencing_and_scoped_recovery(factory):
    with factory.begin() as session:
        repo = StateRepository(session)
        assert finish(repo, attempt(1), 'success')
        assert finish(repo, attempt(2), 'failed')
        assert finish(repo, attempt(3, 'other'), 'success')
        current = repo.read((KEY,))[KEY]
        assert current.status == 'failed' and current.scope == {'window':'window1'}
        assert not repo.begin(attempt(1))
        assert finish(repo, attempt(4), 'success')
        assert repo.read((KEY,))[KEY].status == 'success'
        assert repo.begin(attempt(5))
        assert repo.begin(attempt(6))
        assert not repo.finish(attempt(5), Evidence('unrecorded'))
        assert repo.read((KEY,))[KEY].status == 'running'


def test_failure_retains_success_and_history_is_separate(factory):
    with factory.begin() as session:
        repo = StateRepository(session)
        finish(repo, attempt(1), 'success')
        finish(repo, attempt(2), 'failed')
        hist = replace(attempt(3), key=replace(KEY, scope_kind='historical'))
        finish(repo, hist, 'success')
        assert repo.read((KEY,))[KEY].success_at == attempt(1).started_at


def test_rollback_does_not_publish_success(factory):
    with pytest.raises(RuntimeError), factory.begin() as session:
        finish(StateRepository(session), attempt(1), 'success')
        raise RuntimeError('rollback')
    with factory() as session:
        assert KEY not in StateRepository(session).read((KEY,))
