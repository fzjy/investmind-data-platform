"""企业Core→公开六卡→双产品原子发布，仅登记的一次性PG17。"""
from datetime import UTC, date, datetime, timedelta
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import func, select, text
from testcontainers.community.postgres import PostgresContainer

from app.config import Settings
from app.db.models import Security, IncomeStatement, BalanceSheet, CashflowStatement, Classification, ClassificationMember, PageSnapshot, DailyBasic, TradeCalendar
from app.db.session import create_engine_and_session
from app.macro.jobs.macro_publish import MacroPublishJob
from app.macro.jobs.macro_sync_history import SqlMacroSyncHistory
from app.macro.enterprise_contract import enterprise_loader
from app.macro.enterprise import EnterprisePolicy
from app.macro.enterprise_repository import load_enterprise_inputs
from app.macro.receivables_preview import preview_receivables, _digest
from app.macro.receivables_match import match_receivable
from app.macro.receivables_store import WITNESS_VERSION
from app.db.models.enterprise_receivables import ReceivableAttempt, ReceivableResponse, ReceivableSupplement
from app.stocks.universes import UniverseResolver
from backend.tests.macro.receivables_samples import full_inputs, response_for
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.marts.publication import SnapshotPublisher
from backend.tests.jobs.test_macro_publish import CATALOG
from backend.tests.marts.test_macro_derivations import row

pytestmark=[pytest.mark.postgres,pytest.mark.skipif(os.environ.get('DANG_RUN_POSTGRES_TESTS')!='1',reason='registered disposable PG17 only')]
ROOT=Path(__file__).resolve().parents[3]
AS_OF=datetime(2024,5,31,10,tzinfo=UTC)


def test_pg_real_enterprise_loader_and_atomic_pair_recover_without_losing_previous_version(monkeypatch):
    with PostgresContainer('postgres:17-alpine',driver='psycopg') as pg:
        url=pg.get_connection_url(driver='psycopg')
        monkeypatch.setenv('DANG_DATABASE_URL',url)
        monkeypatch.setenv('DANG_DATABASE_READ_URL',url)
        monkeypatch.setenv('DANG_DATA_BACKEND','database')
        command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head')
        engine,factory=create_engine_and_session(url)
        try:
            with factory.begin() as s:
                assert int(s.scalar(text('SHOW server_version_num')))//10000==17
                s.add(Security(ts_code='000001.SZ',symbol='000001',name='Controlled',list_date=date(2000,1,1)))
                s.flush()
                for year,revenue,profit,stock in ((2023,100,20,100),(2024,110,10,120)):
                    s.add(IncomeStatement(ts_code='000001.SZ',end_date=date(year,3,31),ann_date=date(year,4,30),report_type='1',comp_type='1',data={'revenue':revenue,'n_income_attr_p':profit,'oper_cost':55 if year==2024 else 80,'n_income':100000000}))
                    witness = next(r.values for r in full_inputs().financials if r.table == 'balance')
                    s.add(BalanceSheet(ts_code='000001.SZ',end_date=date(year,3,31),ann_date=date(year,4,30),report_type='1',comp_type='1',data={**witness,'inventories':stock,'contract_liab':100 if year==2023 else 150}))
                    s.add(CashflowStatement(ts_code='000001.SZ',end_date=date(year,3,31),ann_date=date(year,4,30),report_type='1',comp_type='1',
                        data={'n_cashflow_act':300000000 if year==2024 else 200000000,'c_pay_acq_const_fiolta':100000000 if year==2024 else 50000000,
                            'c_pay_dist_dpcp_int_exp':200000000 if year==2024 else 50000000}))
                nodes=[Classification(provider='tushare',source='SW2021',classification_id=code,name=code,level=level,parent_id=parent)
                    for code,level,parent in (('801120.SI',1,None),('801121.SI',2,'801120.SI'),('851121.SI',3,'801121.SI'))]
                s.add_all(nodes);s.flush()
                s.add(ClassificationMember(ts_code='000001.SZ',classification_id=nodes[-1].id,effective_from=date(2000,1,1)))
                s.add_all(TradeCalendar(exchange='SSE',cal_date=date(2024,5,d),is_open=d==6) for d in range(1,7))
                s.add(DailyBasic(ts_code='000001.SZ',trade_date=date(2024,5,6),total_mv=10000,circ_mv=999))
                s.add_all(row(item.id,'2024-04-01',1,released=AS_OF-timedelta(days=1)) for item in CATALOG.series)
            settings=Settings(database_url=url,data_backend='database',macro_enterprise_min_coverage=1,macro_enterprise_min_samples=1)
            base=load_enterprise_inputs(factory,UniverseResolver().resolve('representative-50'))
            plan=preview_receivables(base,AS_OF.date(),EnterprisePolicy(min_samples=1,min_coverage=1),8,100)
            assert len(plan['requests'])==2
            # 仅隔离PG的受控历史夹具，显式插入时间，不修改业务库时间/不可变行。
            with factory.begin() as s:
                for request in plan['requests']:
                    source=next(r for r in base.financials if r.source_key==request['coreBinding']['sourceKey'])
                    response=response_for(source,100 if source.end_date.year==2023 else 150)
                    outcome=match_receivable(request,source,response,5000)
                    assert outcome['status']=='matched'
                    identifier=str(uuid4()); when=AS_OF-timedelta(days=1)
                    s.add(ReceivableAttempt(id=identifier,run_id='controlled-white',request_key=request['requestKey'],
                        plan_fingerprint=plan['planFingerprint'],request=request,page_size=5000,state='received',created_at=when,finished_at=when))
                    s.flush()
                    payload=response.model_dump(mode='json')
                    s.add(ReceivableResponse(attempt_id=identifier,request_key=request['requestKey'],payload=payload,response_hash=_digest(payload),captured_at=when))
                    s.flush()
                    s.add(ReceivableSupplement(request_key=request['requestKey'],response_id=identifier,core_binding=request['coreBinding'],
                        witness_version=WITNESS_VERSION,status=outcome['status'],value=outcome['value'],created_at=when))
            history=SqlMacroSyncHistory(factory); publisher=SnapshotPublisher(factory)
            builder=MacroMartBuilder(MacroRepository(factory),catalog=CATALOG,enterprise_loader=enterprise_loader(factory,settings))
            job=MacroPublishJob(builder,publisher,history)
            def publish(seconds):
                when=AS_OF+timedelta(seconds=seconds)
                run=history.start(f'enterprise:{seconds}',when,CATALOG.catalog_version,CATALOG.catalog_hash,'test',1)
                return job.publish(run,when,CATALOG,(),reuse_unchanged=True)
            first=publish(0)
            assert first.status=='completed' and len(first.promoted_products)==2
            data=publisher.current('macro-transmission').payload['enterpriseAnalysis']
            assert data['poolCount']==50
            assert data['algorithmVersion']=='enterprise-quarter-v7'
            assert data['rea']['version']=='macro-enterprise-rea-v2'
            rea={m['id']:m for m in data['rea']['series']}
            assert rea['lying-flat']['points'][-1]['primary']==100
            assert rea['lying-flat']['points'][-1]['secondary']==50
            assert rea['lying-flat']['points'][-1]['value']==-50
            assert rea['dividend']['points'][-1]['value']==200
            assert rea['capacity']['points']==[]  # 未提供固定资产，不借营收填节点。
            pricing,conversion=[m['points'][-1] for m in data['ledger']['metrics'][:2]]
            orders=data['ledger']['metrics'][2]['points'][-1]
            assert orders['value']==pytest.approx(50) and orders['revenueYoY']==pytest.approx(10)
            assert orders['orderRevenueSpread']==pytest.approx(40)
            assert pricing['value']==pytest.approx(30) and pricing['sampleCount']==1
            assert conversion['amounts']=={'operatingCashflow':3,'netIncome':1} and conversion['value']==3
            scissors,inventory,white,cash,distribution,loss=[c['points'][-1] for c in data['cards']]
            assert (scissors['primary'],scissors['secondary'],scissors['result'],scissors['sampleCount'],scissors['candidateCount'])==(10,-50,60,1,50)
            assert inventory['secondary']==20 and inventory['result']==-10
            assert (white['primary'],white['secondary'],white['result'],white['sampleCount'])==(50,10,40,1)
            assert (cash['primary'],cash['secondary'],cash['result'],cash['sampleCount'],cash['candidateCount'])==(3,1,2,1,50)
            assert cash['capitalExpenditureRatio']==pytest.approx(100/3) and cash['ratioStatus']=='ready'
            assert (distribution['primary'],distribution['secondary'],distribution['result'],distribution['distributionBase'],distribution['distributionYoY'])==(1,2,-1,0.5,300)
            assert distribution['distributionYoYStatus']=='ready'
            assert (loss['primary'],loss['secondary'],loss['result'],loss['totalMarketValue'],loss['valuationDate'])==(0,100,-100,1,'2024-05-06')
            assert publisher.current('macro-insights').version==publisher.current('macro-transmission').version
            def snapshot_state():
                with factory() as s:
                    return ([(p.product,p.version,p.payload,p.published_at) for p in publisher.current_macro_pair()],s.scalar(select(func.count()).select_from(PageSnapshot)))
            before=snapshot_state()
            with factory.begin() as s:
                s.execute(text('ALTER TABLE raw.enterprise_receivable_response DISABLE TRIGGER immutable_enterprise_receivable'))
            with pytest.raises(ValueError,match='ENTERPRISE_SUPPLEMENT_SCHEMA_REQUIRED'):
                publish(1)
            assert snapshot_state()==before
            with factory.begin() as s:
                s.execute(text('ALTER TABLE raw.enterprise_receivable_response ENABLE TRIGGER immutable_enterprise_receivable'))
            with factory.begin() as s:
                target=s.scalar(select(IncomeStatement).where(IncomeStatement.end_date==date(2024,3,31)))
                target.data={'revenue':120,'n_income_attr_p':10,'oper_cost':55,'n_income':100000000}
            original=publisher._write_page_snapshot
            def fail_second(session,candidate,version):
                if candidate.product=='macro-insights': raise RuntimeError('controlled pair failure')
                return original(session,candidate,version)
            monkeypatch.setattr(publisher,'_write_page_snapshot',fail_second)
            with pytest.raises(RuntimeError,match='controlled pair failure'): publish(1)
            assert snapshot_state()==before
            monkeypatch.setattr(publisher,'_write_page_snapshot',original)
            recovered=publish(2)
            assert recovered.version!=first.version and len(recovered.promoted_products)==2
            recovered_white=publisher.current('macro-transmission').payload['enterpriseAnalysis']['cards'][2]['points'][-1]
            assert recovered_white['result']==30
            after=snapshot_state()
            repeated=publish(3)
            assert repeated.version==recovered.version and repeated.promoted_products==() and snapshot_state()==after
        finally:
            engine.dispose()
