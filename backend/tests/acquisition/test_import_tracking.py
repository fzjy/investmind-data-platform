from datetime import UTC, datetime
from types import SimpleNamespace
from app.acquisition.adapters.imports import record_import
from app.acquisition.contracts import StateKey
from app.acquisition.repository import StateRepository


def test_import_proves_only_accepted_source(factory):
    now = datetime.now(UTC)
    with factory.begin() as s:
        record_import(s, [('external-macro','example')], SimpleNamespace(id=1,file_hash='hash'),now)
        key = StateKey('external-macro','','source','example')
        assert StateRepository(s).read((key,))[key].references == ({'fileId':1},)
