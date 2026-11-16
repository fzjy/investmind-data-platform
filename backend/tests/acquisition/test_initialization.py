from datetime import UTC, datetime, timedelta
from pathlib import Path
from app.acquisition.contracts import Attempt, Evidence, EvidenceRecord, StateKey
from app.acquisition.initialization import apply_initialization, plan_initialization
from app.acquisition.repository import StateRepository
from app.db.models import DailyPrice


def test_no_evidence_from_existing_values(stock_factory):
    factory = stock_factory
    now = datetime.now(UTC)
    with factory.begin() as s:
        s.add(DailyPrice(ts_code='600519.SH',trade_date=now.date(),close=20))
    report = plan_initialization(factory,root=Path(__file__).resolve().parents[3],now=now,limit=5,cursor=None)
    assert report.records == ()


def test_old_event_does_not_replace_new_failure(factory):
    key = StateKey('stock-daily','','security','600519.SH')
    old = datetime.now(UTC)-timedelta(days=1)
    now = datetime.now(UTC)
    with factory.begin() as s:
        repo = StateRepository(s)
        attempt = Attempt(key,'live',now,{},'manual')
        repo.begin(attempt)
        repo.finish(attempt,Evidence('failed',now,None,'REQUEST_FAILED'))
        row = EvidenceRecord(Attempt(key,'historical-proof',old,{},'initialization'),Evidence('success',old,old))
        assert apply_initialization(s,(row,))['written'] == 0
        assert repo.read((key,))[key].status == 'failed'
