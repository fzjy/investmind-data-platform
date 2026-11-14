"""一次补跑内所有供应商共用的物理HTTP预算；资源生命周期由外层持有。"""
from threading import Lock

import httpx

from app.connectors.macro.contracts import ProviderTransientError


class HttpRequestBudget:
    def __init__(self, limit: int) -> None:
        if type(limit) is not int or limit < 1:
            raise ValueError('MACRO_CATCHUP_REQUEST_LIMIT_INVALID')
        self.limit = limit
        self._used = 0
        self._lock = Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    def consume(self) -> None:
        with self._lock:
            if self._used >= self.limit:
                raise ProviderTransientError('MACRO_CATCHUP_REQUEST_BUDGET_EXHAUSTED', request_count=0)
            self._used += 1


class BudgetTransport(httpx.BaseTransport):
    def __init__(self, transport: httpx.BaseTransport, budget: HttpRequestBudget) -> None:
        self._transport = transport
        self._budget = budget

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._budget.consume()
        return self._transport.handle_request(request)

    def close(self) -> None:
        # 多个临时client共用同一transport；只有创建它的外层才关闭资源。
        pass
