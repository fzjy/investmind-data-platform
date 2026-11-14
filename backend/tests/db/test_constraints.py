from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import DailyPrice


@pytest.fixture
def session() -> Session:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        execution_options={
            "schema_translate_map": {
                "raw": None,
                "core": None,
                "ops": None,
            }
        },
    )
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def test_daily_price_is_unique_per_security_and_trade_date(session: Session) -> None:
    row = DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 14), close=1488.2)
    session.add(row)
    session.flush()

    session.add(DailyPrice(ts_code="600519.SH", trade_date=date(2026, 8, 14), close=1490.0))
    with pytest.raises(IntegrityError):
        session.flush()
