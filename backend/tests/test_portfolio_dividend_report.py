from datetime import date
from decimal import Decimal

from sqlalchemy import select
from typer.testing import CliRunner

from app.db.models import Dividend, IngestionBatch, SourcePayload

from backend.tests.marts.test_content_runtime import prepare, state


def run(factory):
    from app.portfolio.dividend_report import preview_portfolio_dividends
    return preview_portfolio_dividends(factory)


def test_report_uses_current_published_scope_and_does_not_write(pg_factory):
    factory=pg_factory;prepare(factory)
    before=state(factory)
    report=run(factory)
    assert report['status']=='review_required' and report['requestCount']==0
    assert report['asOf']=='2026-08-24' and report['symbolCount']==1
    assert report['stocks'][0]['symbol']=='600519.SH'
    assert report['stocks'][0]['historyCoverage']=='not_proven'
    assert report['stocks'][0]['economicsReady'] is False
    assert state(factory)==before


def test_cash_fact_and_distribution_zero_do_not_prove_whole_window_capital(pg_factory):
    factory=pg_factory;prepare(factory)
    with factory.begin() as s:
        for row in s.scalars(select(Dividend)):s.delete(row)
        s.add(Dividend(ts_code='600519.SH',end_date=date(2025,12,31),ann_date=date(2026,5,1),div_proc='实施',
            cash_div_tax=Decimal('1'),record_date=date(2026,6,1),ex_date=date(2026,6,2),pay_date=date(2026,6,3),
            data={'imp_ann_date':'20260531','stk_div':0,'stk_bo_rate':0,'stk_co_rate':0,'base_date':'20260601','base_share':100}))
    row=run(factory)['stocks'][0]
    assert row['cashEventCount']==1 and row['capitalFieldsCompleteEvents']==1
    assert row['economicsReady'] is False and row['capitalWindowVerified'] is False


def test_no_screener_does_not_fall_back_to_fixture_or_full_market(pg_factory):
    report=run(pg_factory)
    assert report['status']=='blocked' and report['code']=='SCREENER_BASE_REQUIRED'
    assert report['requestCount']==0


def test_cli_report_has_no_publish_or_capture_switch(monkeypatch):
    from app.cli.main import app
    from app.config import Settings
    import app.cli.portfolio_rebuild as module
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls:Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test')))
    class Engine:
        def dispose(self):pass
    monkeypatch.setattr(module,'create_engine_and_session',lambda url:(Engine(),'factory'))
    monkeypatch.setattr(module,'preview_portfolio_dividends',lambda factory:{'status':'review_required','requestCount':0})
    assert CliRunner().invoke(app,['preview-portfolio-dividends']).exit_code==0
    assert CliRunner().invoke(app,['preview-portfolio-dividends','--publish']).exit_code==2


def test_outside_scope_raw_fields_cannot_hide_current_stock_field_gaps(pg_factory):
    factory=pg_factory;prepare(factory)
    with factory.begin() as s:
        s.add(IngestionBatch(id='outside',source='tushare',dataset='dividend',status='completed'))
        s.add(SourcePayload(batch_id='outside',api_name='dividend',request_hash='outside',content_hash='outside',
            payload={'fields':['ts_code','stk_div','base_share'],'items':[['OTHER',0,100]]}))
    report=run(factory)
    assert 'stk_div' in report['missingRawFields'] and 'base_share' in report['missingRawFields']
    assert 'stk_div' in report['stocks'][0]['missingRawFields']


def test_legacy_core_integer_implementation_date_is_adapted_not_rejected(pg_factory):
    factory=pg_factory;prepare(factory)
    with factory.begin() as s:
        for row in s.scalars(select(Dividend)):s.delete(row)
        s.add(Dividend(ts_code='600519.SH',end_date=date(2025,12,31),ann_date=date(2026,5,1),div_proc='实施',
            cash_div_tax=Decimal('1'),record_date=date(2026,6,1),ex_date=date(2026,6,2),pay_date=date(2026,6,3),
            data={'imp_ann_date':20260531,'stk_div':0,'stk_bo_rate':0,'stk_co_rate':0,'base_date':'','base_share':100}))
    row=run(factory)['stocks'][0]
    assert row['cashEventCount']==1 and row['capitalFieldsCompleteEvents']==0
