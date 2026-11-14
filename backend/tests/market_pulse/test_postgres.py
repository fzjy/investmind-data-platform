"""登记隔离PG验证共同入库行锁，禁止连接业务库。"""
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.connectors.tushare.contracts import TushareResponse
from app.db.models import DailyBasic
from app.ingestion.repositories import IngestionRepository
from app.market.pulse.repository import PulseRepository, FIELDS
from backend.tests.marts.test_content_runtime_postgres import database

pytestmark = [pytest.mark.postgres, pytest.mark.skipif(
    os.environ.get('DANG_RUN_POSTGRES_TESTS') != '1', reason='registered disposable PG required')]


def test_pg_ordinary_basic_writer_waits_for_field_owner_row_lock(database):
    _, factory, _ = database
    with factory() as session:
        basic = session.scalar(select(DailyBasic))
        code, day = basic.ts_code, basic.trade_date
    now = datetime(2026, 10, 16, 16, tzinfo=UTC)
    repo = PulseRepository(factory)
    assert repo.persist(code, day, 'controlled', TushareResponse(fields=FIELDS,
        items=((code, day.strftime('%Y%m%d'), 0),)), now)
    with factory.begin() as owner:
        owner.scalar(select(DailyBasic).filter_by(ts_code=code, trade_date=day).with_for_update())
        with pytest.raises(OperationalError):
            with factory.begin() as writer:
                writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                IngestionRepository(writer).upsert(DailyBasic, {'ts_code': code, 'trade_date': day},
                    {'data': {'pe': 24}})
    with factory.begin() as writer:
        IngestionRepository(writer).upsert(DailyBasic, {'ts_code': code, 'trade_date': day}, {'data': {'pe': 24}})
    assert repo.is_complete(code, day, 'controlled', now)
