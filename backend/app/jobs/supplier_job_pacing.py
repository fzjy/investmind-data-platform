"""同一入口内串行供应商作业；补足不同客户端之间的请求间隔。"""
from contextlib import contextmanager
from threading import RLock, local
import time


class SupplierJobPacer:
    def __init__(self, requests_per_minute, *, clock=time.monotonic, sleep=time.sleep):
        if requests_per_minute <= 0:
            raise ValueError('positive RPM required')
        self._interval = 60 / requests_per_minute
        self._clock, self._sleep = clock, sleep
        self._lock, self._thread = RLock(), local()
        self._finished = None

    def before_send(self):
        """供有真实发送回调的独立组使用；0HTTP路径不调用、不等待。"""
        with self._lock:
            if self._finished is not None:
                remaining = self._interval - (self._clock() - self._finished)
                if remaining > 0:
                    self._sleep(remaining)
            self._finished = self._clock()

    @contextmanager
    def scope(self):
        with self._lock:
            depth = getattr(self._thread, 'depth', 0)
            self._thread.depth = depth + 1
            try:
                if depth == 0 and self._finished is not None:
                    remaining = self._interval - (self._clock() - self._finished)
                    if remaining > 0:
                        self._sleep(remaining)
                yield
            finally:
                self._thread.depth = depth
                if depth == 0:
                    self._finished = self._clock()
