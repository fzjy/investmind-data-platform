"""只用登记隔离PG检验真实行锁及旧writer保护。"""
import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.connectors.tushare.contracts import TushareResponse
from app.db.models import Security
from app.ingestion.repositories import IngestionRepository
from app.market.region.repository import RegionRepository, FIELDS
from backend.tests.marts.test_content_runtime_postgres import database

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG required')]


def test_ordinary_security_writer_waits_then_preserves_owned_region(database):
    _,factory,_=database
    with factory() as session: code=session.scalar(select(Security.ts_code))
    now=datetime.now(UTC)
    repo=RegionRepository(factory)
    assert repo.persist(code,'controlled',TushareResponse(fields=FIELDS,items=((code,'贵州'),)),now)
    with factory.begin() as owner:
        owner.get(Security,code,with_for_update=True)
        with pytest.raises(OperationalError):
            with factory.begin() as writer:
                writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                IngestionRepository(writer).upsert(Security,{'ts_code':code},{'data':{'name':'new'}})
    with factory.begin() as writer:
        IngestionRepository(writer).upsert(Security,{'ts_code':code},{'data':{'name':'new'}})
    assert repo.is_complete(code,'controlled',now,30)


def test_region_owner_reloads_existing_security_after_other_writer(database):
    _,factory,_=database
    with factory() as session: code=session.scalar(select(Security.ts_code))
    # 独立补齐只合并两个JSON字段，不擦掉已经提交的证券资料。
    with factory.begin() as session:
        security=session.get(Security,code)
        security.data={**security.data,'foreignMetadata':{'keep':1}}
    now=datetime.now(UTC)
    assert RegionRepository(factory).persist(code,'controlled',TushareResponse(fields=FIELDS,items=((code,'深圳'),)),now)
    with factory() as session:
        assert session.get(Security,code).data['foreignMetadata']=={'keep':1}
