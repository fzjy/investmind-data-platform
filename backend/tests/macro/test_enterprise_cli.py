from backend.tests.support.pg_snapshot import database_snapshot
from backend.tests.support.configuration import assert_fixture_configuration_rejected
from datetime import date
import importlib
import json

import pytest
from sqlalchemy import select, func
from typer.testing import CliRunner

from app.config import Settings
from app.db.models import Security, Classification, ClassificationMember, IncomeStatement, BalanceSheet
from app.cli.main import app


@pytest.fixture
def database(pg_factory, pg_schema_url, monkeypatch):
    factory = pg_factory
    url = pg_schema_url
    with factory.begin() as session:
        session.add(Security(ts_code='000001.SZ',symbol='000001',name='Controlled ST',list_date=date(2020,1,1),is_st=True))
        root=Classification(provider='tushare',source='SW2021',classification_id='801120.SI',name='消费',level=1)
        child=Classification(provider='tushare',source='SW2021',classification_id='801121.SI',name='测试二级',level=2,parent_id='801120.SI')
        leaf=Classification(provider='tushare',source='SW2021',classification_id='851121.SI',name='测试三级',level=3,parent_id='801121.SI')
        session.add_all((root,child,leaf));session.flush()
        session.add(ClassificationMember(ts_code='000001.SZ',classification_id=leaf.id,effective_from=date(2020,1,1)))
        for year,revenue,profit,inventory in ((2023,100,10,100),(2024,110,5,120)):
            values={'ann_date':f'{year}0430','end_date':f'{year}0331'}
            session.add(IncomeStatement(ts_code='000001.SZ',end_date=date(year,3,31),ann_date=date(year,4,30),report_type='1',comp_type='1',update_flag='1',data={**values,'revenue':revenue,'n_income_attr_p':profit}))
            session.add(BalanceSheet(ts_code='000001.SZ',end_date=date(year,3,31),ann_date=date(year,4,30),report_type='1',comp_type='1',update_flag='1',data={**values,'inventories':inventory}))
    monkeypatch.setenv('DANG_DATA_BACKEND','database')
    monkeypatch.setenv('DANG_DATABASE_URL',url)
    monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
    monkeypatch.setenv('DANG_SYNC_UNIVERSE','representative-50')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_MIN_COVERAGE','1')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_MIN_SAMPLES','1')
    return factory.kw['bind'], factory


def report(result):
    return next(json.loads(line) for line in result.stdout.splitlines() if line.startswith('{') and 'schemaVersion' in line)


def test_cli_really_reads_core_and_keeps_database_unchanged(database):
    db_engine,factory=database
    before=database_snapshot(db_engine)
    result=CliRunner().invoke(app,['audit-macro-enterprise'])
    assert result.exit_code==0,result.stdout
    data=report(result)
    assert data['poolCount']==50 and data['policy']['min_samples']==1
    assert data['algorithmVersion']=='enterprise-quarter-v7'
    assert data['rea']['version']=='macro-enterprise-rea-v2'
    assert data['rea']['depreciationMode']=='reported-quarter-priority-explicit-estimates-v1'
    assert data['rea']['poolCount']==50
    assert len(data['rea']['series'])==10
    assert [m['id'] for m in data['ledger']['metrics']]==['pricing-power','cash-conversion','order-temperature']
    assert all(not m['points'] for m in data['ledger']['metrics'])
    assert [c['id'] for c in data['cards']]==['scissors','inventory','white-strip','lying-flat','dividend','loss']
    assert all(c['points']==[] for c in data['cards'][-3:])  # 缺现金流/交易日历不伪造金额或市值卡。
    point=data['cards'][0]['points'][-1]
    assert (point['primary'],point['secondary'],point['result'],point['sampleCount'])==(10.0,-50.0,60.0,1)
    assert data['readOnly'] is True
    assert database_snapshot(db_engine)==before


def test_repository_parent_path_and_st_are_from_actual_core(database):
    _,factory=database
    try:
        loader=importlib.import_module('app.macro.enterprise_repository')
    except ImportError:
        pytest.fail('enterprise core adapter missing')
    from app.stocks.universes import UniverseResolver
    loaded=loader.load_enterprise_inputs(factory,UniverseResolver().resolve('representative-50'))
    assert len(loaded.symbols)==50
    assert loaded.industries[0].root_code=='801120.SI'
    assert len(loaded.financials)==4
    assert len(loaded.securities)==1


def test_loss_adapter_loads_only_fixed_valuation_dates_and_does_not_pollute_preview(database):
    from dataclasses import replace
    from app.db.models import DailyBasic, TradeCalendar
    from app.macro import enterprise_repository as repository
    from app.macro.enterprise import EnterprisePolicy
    from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis
    from app.stocks.universes import UniverseResolver
    db_engine,factory=database
    with factory.begin() as s:
        s.add_all(TradeCalendar(exchange='SSE',cal_date=date(2024,5,d),is_open=d==6) for d in range(1,8))
        s.add_all(DailyBasic(ts_code='000001.SZ',trade_date=date(2024,5,d),total_mv=10000 if d==6 else 99999) for d in (6,7))
    before=database_snapshot(db_engine)
    base=repository.load_enterprise_inputs(factory,UniverseResolver().resolve('representative-50'))
    assert not base.loss_enabled and not base.market_values and not base.trading_days
    policy=EnterprisePolicy(min_samples=1,min_coverage=1,max_quarters=1)
    assert hasattr(repository,'load_enterprise_loss_inputs'), 'loss adapter missing'
    loaded=repository.load_enterprise_loss_inputs(factory,replace(base,cashflow_enabled=True,distribution_enabled=True),date(2024,5,31),policy)
    assert {r.trade_date for r in loaded.market_values}=={date(2024,5,6)} and len(loaded.trading_days)==6
    value=public_enterprise_analysis(loaded,date(2024,5,31),policy)
    p=value['cards'][-1]['points'][-1]
    assert (p['primary'],p['secondary'],p['result'],p['totalMarketValue'])==(0,100,-100,1)
    assert valid_enterprise_analysis(value) and database_snapshot(db_engine)==before


def test_cli_fixture_mode_refused_without_writing(monkeypatch):
    assert_fixture_configuration_rejected(monkeypatch)


def test_cli_unavailable_connection_does_not_disclose_dsn(monkeypatch):
    monkeypatch.setenv('DANG_DATA_BACKEND','database')
    monkeypatch.setenv('DANG_DATABASE_URL','invalid://private:password@private-host/db')
    result=CliRunner().invoke(app,['audit-macro-enterprise'])
    assert result.exit_code==2
    assert 'ENTERPRISE_AUDIT_FAILED' in result.stdout
    assert 'password' not in result.stdout+result.stderr
    assert 'private-host' not in result.stdout+result.stderr


def test_policy_environment_values_are_validated(monkeypatch):
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_MIN_COVERAGE','75')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_MIN_SAMPLES','7')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_MAX_QUARTERS','8')
    settings=Settings.from_env()
    assert getattr(settings,'macro_enterprise_min_coverage',None)==75
    assert getattr(settings,'macro_enterprise_min_samples',None)==7
    assert getattr(settings,'macro_enterprise_max_quarters',None)==8
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_MIN_COVERAGE','101')
    with pytest.raises(ValueError): Settings.from_env()


def test_invalid_timezone_is_a_sanitized_machine_error(database,monkeypatch):
    monkeypatch.setenv('DANG_SYNC_TIMEZONE','Asia/Shanghia')
    result=CliRunner().invoke(app,['audit-macro-enterprise'])
    assert result.exit_code==2
    assert 'ENTERPRISE_AUDIT_FAILED' in result.stdout
