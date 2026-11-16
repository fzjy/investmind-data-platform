from datetime import UTC, datetime
from app.acquisition.adapters.content import record_news
from app.acquisition.contracts import StateKey
from app.acquisition.repository import StateRepository


def test_partial_news_cycle_does_not_confirm_empty(factory):
    now = datetime.now(UTC)
    key = StateKey('news','','source','sina-finance')
    with factory.begin() as s:
        record_news(s,{'lastAttemptAt':now.isoformat(),'nextId':'cursor'},now)
        assert StateRepository(s).read((key,))[key].status == 'unrecorded'
