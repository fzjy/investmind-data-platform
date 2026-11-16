from datetime import UTC, datetime
from app.acquisition.contracts import Attempt, StateKey
from app.acquisition.tracking import record_failed_attempt, recover_attempts, start_attempt
from app.acquisition.repository import StateRepository
from app.db.models import JobRun


def test_recover_only_known_finished_parent(factory):
    now = datetime.now(UTC)
    key = StateKey('stock-dividend','','security','600519.SH')
    attempt = Attempt(key, 'unit1', now, {'runId':'r'}, 'scheduler')
    with factory.begin() as session:
        session.add(JobRun(id='r',job_name='test',business_key='r',status='failed',finished_at=now))
        start_attempt(session, attempt)
    assert recover_attempts(factory, now=now, active_attempt_ids={'unit1'})['recovered'] == 0
    assert recover_attempts(factory, now=now, active_attempt_ids=set())['recovered'] == 1
    with factory() as session:
        assert StateRepository(session).read((key,))[key].status == 'unrecorded'


def test_record_failure_without_committed_start(factory):
    now = datetime.now(UTC)
    key = StateKey('stock-dividend','','security','600519.SH')
    attempt = Attempt(key, 'unit1', now, {}, 'manual')
    record_failed_attempt(factory, attempt, code='STORAGE_FAILED', reason='入库失败', checked_at=now, references=())
    with factory() as session:
        assert StateRepository(session).read((key,))[key].error_code == 'STORAGE_FAILED'
