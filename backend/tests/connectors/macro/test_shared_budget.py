"""多供应商物理发送共用预算，而非逻辑调用计数。"""
import importlib
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.connectors.macro.contracts import ProviderTransientError


def module():
    assert importlib.util.find_spec('app.connectors.macro.budget') is not None, 'missing shared physical HTTP budget'
    return importlib.import_module('app.connectors.macro.budget')


def test_clients_methods_pages_and_failures_consume_one_shared_cap():
    calls=[]
    def handle(request):
        calls.append((request.method,request.url.path))
        return httpx.Response(503 if request.url.path=='/failed' else 200)
    budget=module().HttpRequestBudget(3)
    inner=httpx.MockTransport(handle)
    with httpx.Client(transport=module().BudgetTransport(inner,budget)) as first:
        assert first.get('https://example.test/page1').status_code==200
    with httpx.Client(transport=module().BudgetTransport(inner,budget)) as second:
        assert second.post('https://example.test/failed').status_code==503
        assert second.get('https://example.test/page2').status_code==200
        with pytest.raises(ProviderTransientError,match='MACRO_CATCHUP_REQUEST_BUDGET_EXHAUSTED'):
            second.get('https://example.test/page3')
    assert calls==[('GET','/page1'),('POST','/failed'),('GET','/page2')]
    assert budget.used==3


def test_network_failure_is_one_physical_attempt_not_zero():
    def handle(request): raise httpx.ConnectError('controlled',request=request)
    budget=module().HttpRequestBudget(1)
    with httpx.Client(transport=module().BudgetTransport(httpx.MockTransport(handle),budget)) as client:
        with pytest.raises(httpx.ConnectError): client.get('https://example.test/')
        with pytest.raises(ProviderTransientError): client.get('https://example.test/')
    assert budget.used==1


def test_cap_is_atomic_under_concurrent_senders():
    budget=module().HttpRequestBudget(5)
    def handle(request): return httpx.Response(200)
    with httpx.Client(transport=module().BudgetTransport(httpx.MockTransport(handle),budget)) as client:
        def send(_):
            try: return client.get('https://example.test/').status_code
            except ProviderTransientError: return 'blocked'
        with ThreadPoolExecutor(max_workers=10) as workers: results=list(workers.map(send,range(30)))
    assert results.count(200)==5 and results.count('blocked')==25 and budget.used==5


def test_child_transport_close_does_not_close_the_shared_owner():
    class Owned(httpx.BaseTransport):
        closed=False
        def handle_request(self,request):
            assert not self.closed
            return httpx.Response(200)
        def close(self): self.closed=True
    inner=Owned()
    budget=module().HttpRequestBudget(2)
    with httpx.Client(transport=module().BudgetTransport(inner,budget)) as client: client.get('https://example.test/')
    assert not inner.closed
    with httpx.Client(transport=module().BudgetTransport(inner,budget)) as client: client.get('https://example.test/')
    inner.close()
    assert inner.closed and budget.used==2


@pytest.mark.parametrize('limit',[0,-1,True,1.5])
def test_invalid_budget_is_refused_before_any_http(limit):
    with pytest.raises(ValueError): module().HttpRequestBudget(limit)


def test_tushare_scoped_counter_does_not_count_global_budget_rejection():
    from app.macro.jobs.tushare_macro_sync_runtime import _LimitedTransport
    budget=module().HttpRequestBudget(1)
    scoped=_LimitedTransport(module().BudgetTransport(httpx.MockTransport(lambda r:httpx.Response(200)),budget),6)
    with httpx.Client(transport=scoped) as client:
        client.get('https://example.test/first')
        with pytest.raises(ProviderTransientError): client.get('https://example.test/blocked')
    assert scoped.used==1 and budget.used==1


def test_dr007_month_window_audit_excludes_unsent_budget_blocked_request():
    import json
    from datetime import UTC,date,datetime
    from app.connectors.macro.contracts import MacroFetchWindow
    from app.connectors.macro.tushare import TushareMacroProvider
    from app.connectors.tushare.client import TushareClient
    from app.macro.catalog import MacroSeriesCatalog,DEFAULT_MACRO_CATALOG_FILE
    catalog=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    definition=catalog.require('cn.rate.dr007')
    budget=module().HttpRequestBudget(1)
    def handle(request):
        body=json.loads(request.content)
        fields=body['fields'].split(',')
        return httpx.Response(200,json={'code':0,'data':{'fields':fields,
            'items':[[body['params']['end_date'],'DR007.IB','DR007',1.2]]}})
    with TushareClient('controlled-token',max_retries=0,transport=module().BudgetTransport(httpx.MockTransport(handle),budget)) as client:
        result=TushareMacroProvider(client).fetch(definition,definition.sources[0],
            MacroFetchWindow(date(2026,9,1),date(2026,10,9),datetime(2026,10,11,12,tzinfo=UTC)))
    assert result.status=='failed' and budget.used==1 and result.request_count==1
