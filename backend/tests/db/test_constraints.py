from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import DailyPrice, Security


@pytest.fixture
def session(pg_factory):
    with pg_factory() as value:
        value.add(Security(ts_code="600519.SH", symbol="600519", name="贵州茅台"))
        value.flush()
        yield value


def test_daily_price_is_unique_per_security_and_trade_date(session: Session) -> None:
    row = DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 14), close=1488.2)
    session.add(row)
    session.flush()

    session.add(DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 14), close=1490.0))
    with pytest.raises(IntegrityError):
        session.flush()
