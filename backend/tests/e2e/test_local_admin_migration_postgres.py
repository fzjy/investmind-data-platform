from datetime import UTC, datetime
from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine, inspect, text
from app.config import PROJECT_ROOT, Settings
import app.config as config_module
from app.ops.backup.project_guards import GUARD_COUNTS

@pytest.mark.parametrize('prior', [False, True])
def test_local_admin_upgrade(pg_url, monkeypatch, prior):
    monkeypatch.setattr(config_module, 'settings', Settings(_env_file=None, database_url=pg_url))
    cfg = Config(str(PROJECT_ROOT / 'backend/alembic.ini'))
    engine = create_engine(pg_url)
    if prior:
        command.upgrade(cfg, '20261116_0022')
        with engine.begin() as conn:
            assert not inspect(conn).has_table('local_admin_task', schema='ops')
            assert not inspect(conn).has_table('local_admin_execution', schema='ops')
            conn.execute(text("INSERT INTO mart.published_snapshot(product,version,as_of,payload,stale) VALUES ('upgrade','original',now(),'{}',false)"))
            conn.execute(text("INSERT INTO ops.data_acquisition_state VALUES ('entry','item','security','sample','routine','attempt',now(),false,'{}','test','{}','{}','{}')"))
            evidence = conn.execute(text('SELECT * FROM ops.data_acquisition_state')).all()
            pointer = conn.execute(text('SELECT * FROM mart.published_snapshot')).all()
    command.upgrade(cfg, 'head')
    with engine.begin() as conn:
        for table in ('local_admin_task', 'local_admin_execution'):
            assert inspect(conn).has_table(table, schema='ops')
        assert conn.scalar(text('SELECT version_num FROM alembic_version')) == '20261117_0023'
        assert GUARD_COUNTS['20261117_0023'] == 5
        if prior:
            assert conn.execute(text('SELECT * FROM ops.data_acquisition_state')).all() == evidence
            assert conn.execute(text('SELECT * FROM mart.published_snapshot')).all() == pointer
        conn.execute(text("INSERT INTO ops.local_admin_execution(id,name,generation,recovery,started) VALUES (123,'task',1,false,now())"))
        assert conn.scalar(text('SELECT id FROM ops.local_admin_execution')) == 123
    engine.dispose()
