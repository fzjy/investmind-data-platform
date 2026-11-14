"""生产分片不能截断长停机窗口或返回部分成功值。"""
from datetime import UTC, date, datetime
from decimal import Decimal
import importlib
import importlib.util

import pytest

from app.connectors.macro.contracts import MacroFetchWindow
from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.errors import TushareTransientError
from app.macro.jobs.dr007_history import load_dr007_source
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog

SID = 'cn.rate.dr007'
NOW = datetime(2026, 10, 8, 15, tzinfo=UTC)


def test_current_entry_exists():
    assert importlib.util.find_spec('app.connectors.macro.tushare_dr007_current') is not None


@pytest.fixture
def adapter():
    if importlib.util.find_spec('app.connectors.macro.tushare_dr007_current') is None:
        pytest.skip('current entry not yet implemented')
    return importlib.import_module('app.connectors.macro.tushare_dr007_current').fetch_dr007_current


class Client:
    def __init__(self, failure=0, empty=False):
        self.calls, self.failure, self.empty = [], failure, empty

    def call(self, api, params, fields):
        assert api == 'repo_daily' and fields == ('trade_date','ts_code','repo_maturity','weight')
        assert set(params) == {'ts_code','start_date','end_date'}
        self.calls.append(dict(params))
        if len(self.calls) == self.failure:
            raise TushareTransientError('not logged')
        rows = () if self.empty else ((params['end_date'],'DR007.IB','DR007',1.3822),)
        return TushareResponse(fields=tuple(fields), items=rows)


def fetch(adapter, client, start=date(2026, 8, 1), end=date(2026, 10, 8), params=None):
    source = load_dr007_source().model_copy(update={'params':params or {'ts_code':'DR007.IB','maxPages':'24'}})
    return adapter(client, MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(SID),
        source, MacroFetchWindow(start,end,NOW))


def test_long_window_is_disjoint_bounded_and_returns_every_verified_point(adapter):
    client = Client()
    result = fetch(adapter,client)
    assert result.status == 'success' and result.request_count == 3
    assert [(p['start_date'],p['end_date']) for p in client.calls] == [
        ('20260801','20260831'),('20260901','20261001'),('20261002','20261008')]
    assert [o.observed_at for o in result.observations] == [date(2026,8,31),date(2026,10,1),date(2026,10,8)]
    assert all(o.value == Decimal('1.3822') for o in result.observations)
    assert len(result.raw_payload['pages']) == 3


@pytest.mark.parametrize('failure',[1,2,3])
def test_failed_part_retains_evidence_but_never_returns_partial_observations(adapter,failure):
    result = fetch(adapter,Client(failure=failure))
    assert result.status == 'failed' and result.observations == ()
    assert result.request_count == failure and len(result.raw_payload['pages']) == failure-1


def test_budget_preflight_does_not_send_any_request(adapter):
    client = Client()
    result = fetch(adapter,client,params={'ts_code':'DR007.IB','maxPages':'2'})
    assert result.status == 'failed' and result.error.code == 'DR007_REQUEST_BUDGET_EXCEEDED'
    assert result.request_count == 0 and client.calls == []


@pytest.mark.parametrize('params',[
    {'ts_code':'DR007.IB','maxPages':'0'}, {'ts_code':'DR007.IB','maxPages':'241'},
    {'ts_code':'DR007.IB','maxPages':'2','unexpected':'x'},
    {'ts_code':'R007.IB','maxPages':'24'}, {'ts_code':'DR007.IB','maxPages':'２４'},
])
def test_invalid_contract_fails_without_requests(adapter,params):
    client=Client()
    result=fetch(adapter,client,params=params)
    assert result.status=='failed' and result.request_count==0 and client.calls==[]


def test_empty_current_is_no_change_not_fabricated_success(adapter):
    result=fetch(adapter,Client(empty=True),start=date(2026,10,1))
    assert result.status=='no_change' and result.observations==() and result.request_count==1


def test_provider_dispatches_production_budget_but_historical_contract_remains_single_window():
    from app.connectors.macro.tushare import TushareMacroProvider
    source=load_dr007_source().model_copy(update={'params':{'ts_code':'DR007.IB','maxPages':'24'}})
    result=TushareMacroProvider(Client()).fetch(MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(SID),
        source,MacroFetchWindow(date(2026,8,1),date(2026,10,8),NOW))
    assert result.status=='success' and result.request_count==3
