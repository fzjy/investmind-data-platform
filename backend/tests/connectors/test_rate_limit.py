from __future__ import annotations

import pytest

from app.connectors.tushare.rate_limit import RequestRateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.sleep_calls.append(duration)
        self.now += duration


def test_first_acquire_does_not_sleep() -> None:
    fake_clock = FakeClock()
    limiter = RequestRateLimiter(
        30,
        clock=fake_clock.monotonic,
        sleep=fake_clock.sleep,
    )

    limiter.acquire()

    assert fake_clock.sleep_calls == []


def test_acquire_sleeps_for_remaining_interval() -> None:
    fake_clock = FakeClock()
    limiter = RequestRateLimiter(
        30,
        clock=fake_clock.monotonic,
        sleep=fake_clock.sleep,
    )

    limiter.acquire()
    fake_clock.now += 1.0

    limiter.acquire()

    assert fake_clock.sleep_calls == [1.0]


def test_acquire_does_not_sleep_after_interval_has_elapsed() -> None:
    fake_clock = FakeClock()
    limiter = RequestRateLimiter(
        30,
        clock=fake_clock.monotonic,
        sleep=fake_clock.sleep,
    )

    limiter.acquire()
    fake_clock.now += 2.5

    limiter.acquire()

    assert fake_clock.sleep_calls == []


def test_constructor_rejects_non_positive_request_rates() -> None:
    with pytest.raises(ValueError, match="requests_per_minute must be positive"):
        RequestRateLimiter(0)
