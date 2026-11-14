from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.ops import capacity


def test_startup_uses_total_remaining_budget_and_cleanup_is_separately_bounded(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(capacity.time, "monotonic", lambda: clock[0])

    @contextmanager
    def slow_target(*, deadline=None, cleanup_timeout_seconds=30):
        try:
            # 慢外部启动需10秒：有剩余预算时只能等待到deadline，不许先等完再查。
            if deadline is not None:
                clock[0] = min(clock[0] + 10, deadline)
                raise capacity.CapacityError("capacity_timeout", 3)
            clock[0] += 10
            yield SimpleNamespace(url="postgresql://unused")
        finally:
            clock[0] += min(10, cleanup_timeout_seconds)

    monkeypatch.setattr(capacity, "open_capacity_target", slow_target)
    with pytest.raises(capacity.CapacityError, match="capacity_timeout"):
        capacity.capacity_benchmark(SimpleNamespace(), options=capacity.CapacityOptions(timeout_seconds=1))
    assert clock[0] <= 11  # 原实现先启动10秒再报超时、清理10秒，总计20秒。
