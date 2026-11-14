"""只使用登记的一次性PG17，不连接业务库。"""
from datetime import date
import os
import json
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from testcontainers.community.postgres import PostgresContainer
from typer.testing import CliRunner

from app.cli.main import app
from app.db.session import create_engine_and_session
from app.db.models import Security, IncomeStatement, Classification, ClassificationMember
from app.shared.readonly_database import open_readonly_factory

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]
ROOT=Path(__file__).resolve().parents[3]


def test_pg_enterprise_audit_enforces_readonly_and_cannot_mutate_core(monkeypatch):
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATABASE_URL',url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        monkeypatch.setenv('DANG_DATA_BACKEND','database')
        monkeypatch.setenv('DANG_SYNC_UNIVERSE','representative-50')
        monkeypatch.setenv('DANG_MACRO_ENTERPRISE_MIN_SAMPLES','1')
        monkeypatch.setenv('DANG_MACRO_ENTERPRISE_MIN_COVERAGE','1')
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head')
        engine,factory=create_engine_and_session(url)
        try:
            with factory.begin() as s:
                s.add(Security(ts_code='000001.SZ',symbol='000001',name='Controlled',list_date=date(2000,1,1)))
                s.flush()
                s.add(IncomeStatement(ts_code='000001.SZ',end_date=date(2024,3,31),ann_date=date(2024,4,30),report_type='1',comp_type='1',data={'revenue':110,'n_income_attr_p':10}))
                s.add(IncomeStatement(ts_code='000001.SZ',end_date=date(2023,3,31),ann_date=date(2023,4,30),report_type='1',comp_type='1',data={'revenue':100,'n_income_attr_p':20}))
                root=Classification(provider='tushare',source='SW2021',classification_id='801120.SI',name='消费',level=1)
                child=Classification(provider='tushare',source='SW2021',classification_id='801121.SI',name='测试二级',level=2,parent_id='801120.SI')
                leaf=Classification(provider='tushare',source='SW2021',classification_id='851121.SI',name='测试三级',level=3,parent_id='801121.SI')
                s.add_all((root,child,leaf));s.flush()
                s.add(ClassificationMember(ts_code='000001.SZ',classification_id=leaf.id,effective_from=date(2000,1,1)))
            with open_readonly_factory(url) as readonly,readonly() as s:
                assert s.scalar(text('SHOW transaction_read_only'))=='on'
                assert s.scalar(text('SHOW transaction_isolation'))=='repeatable read'
                with pytest.raises(DBAPIError):
                    s.execute(text("UPDATE core.security SET name='forbidden' WHERE ts_code='000001.SZ'"))
            result=CliRunner().invoke(app,['audit-macro-enterprise'])
            assert result.exit_code==0,result.stdout
            assert '"readOnly": true' in result.stdout and '"poolCount": 50' in result.stdout
            data=next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{') and 'schemaVersion' in line)
            assert data['cards'][0]['points'], 'PG must verify actual nonempty comparable calculation'
            point=data['cards'][0]['points'][-1]
            assert (point['primary'],point['secondary'],point['result'],point['sampleCount'],point['candidateCount'])==(10.0,-50.0,60.0,1,50)
            with factory() as s:
                assert s.scalar(text("SELECT name FROM core.security WHERE ts_code='000001.SZ'"))=='Controlled'
                assert s.scalar(text('SELECT count(*) FROM ops.job_run'))==0
                assert s.scalar(text('SELECT count(*) FROM mart.published_snapshot'))==0
            # 同一登记隔离PG继续验证新版折旧消费；替换的仅是本测试容器内受控财报。
            from app.db.models import BalanceSheet, CashflowStatement
            from app.config import Settings
            from app.macro.enterprise_contract import enterprise_loader, valid_enterprise_analysis
            from backend.tests.macro.test_renewal_estimation import enabled
            from math import isclose
            with factory.begin() as s:
                s.query(IncomeStatement).delete()
                for row in enabled(2).financials:
                    if row.symbol!='000001.SZ':continue
                    model={'income':IncomeStatement,'balance':BalanceSheet,'cashflow':CashflowStatement}[row.table]
                    s.add(model(ts_code=row.symbol,end_date=row.end_date,ann_date=row.ann_date,
                        report_type=row.report_type,comp_type=row.comp_type,update_flag=row.update_flag,data=row.values))
            def financial_rows(session):
                return [list(session.execute(text(f'SELECT to_jsonb(t)::text FROM core.{table} t ORDER BY id')).scalars())
                    for table in ('income_statement','balance_sheet','cashflow_statement')]
            with factory() as s:before=financial_rows(s)
            with open_readonly_factory(url) as readonly:
                normal=enterprise_loader(readonly,Settings.from_env())(date(2024,8,1))
                assert valid_enterprise_analysis(normal)
                m=next(m for m in normal['rea']['series'] if m['id']=='renewal')
                p=next(p for p in m['points'] if p['period']=='2024-06-30')
                q=next(q for q in m['quality'] if q['period']==p['period'])
                assert normal['rea']['version']=='macro-enterprise-rea-v2' and isclose(p['value'],8/3)
                assert q['depreciationEvidence']['estimatedMembers']==['000001.SZ']
            with factory() as s:
                assert financial_rows(s)==before
                assert s.scalar(text('SELECT count(*) FROM ops.job_run'))==0
                assert s.scalar(text('SELECT count(*) FROM mart.published_snapshot'))==0
        finally:
            engine.dispose()
