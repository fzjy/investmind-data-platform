import pytest
from datetime import UTC, datetime
from app.acquisition.contracts import Attempt, Evidence, StateKey
from app.acquisition.repository import StateRepository

def test_reject_sensitive_scope_before_writing(factory):
    key = StateKey('stock-daily','','security','600519.SH')
    with factory.begin() as session, pytest.raises(ValueError):
        StateRepository(session).begin(Attempt(key,'a',datetime.now(UTC),{'params':{'token':'should-not-persist'}},'test'))
