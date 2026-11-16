from backend.tests.support.configuration import assert_fixture_configuration_rejected
from copy import deepcopy
from sqlalchemy import select
from typer.testing import CliRunner
from app.config import Settings
from app.db.models import PublishedSnapshot
from app.jobs.locks import ProcessLocalJobLock,data_publication_lock_key
from app.stocks.marts.stock_research_runtime import rebuild_stock_research

from backend.tests.marts.test_content_runtime import prepare,state

def old_stock(factory):
    prepare(factory)
    with factory.begin() as session:
        p=session.get(PublishedSnapshot,'screener');payload=deepcopy(p.payload)
        for row in payload['stocks']:
            row.pop('researchValues',None);row.pop('financialTrends',None)
        p.payload=payload

def test_rebuild_default_dry_run_is_read_only_and_actual_scope(pg_factory):
    factory=pg_factory;old_stock(factory);before=state(factory)
    report=rebuild_stock_research(factory)
    assert report['status']=='planned' and report['wouldPublish'] and report['symbolCount']==1 and report['workbenchCount']==1
    assert report['requestCount']==0 and state(factory)==before


def test_operating_history_is_built_after_statement_overlay_and_published_without_other_changes(pg_factory):
    from app.stocks.operating_history import operating_history
    factory=pg_factory;old_stock(factory)
    assert rebuild_stock_research(factory,dry_run=False)['status']=='completed'
    with factory() as session:
        snapshot=session.get(PublishedSnapshot,'stock-workbench:600519.SH')
        payload=snapshot.payload;as_of=snapshot.as_of.date()
    dashboard=payload['financial']['dashboard']
    assert dashboard['operatingHistory']==operating_history(payload['financial']['statements']['tables'],as_of)
    again=rebuild_stock_research(factory,dry_run=False)
    assert again['status']=='no_change' and again['requestCount']==0

def test_rebuild_only_promotes_stock_pair_and_reentry_does_not_promote(pg_factory):
    factory=pg_factory;old_stock(factory);before=state(factory)
    report=rebuild_stock_research(factory,dry_run=False)
    assert report['status']=='completed' and set(report['promotedProducts'])=={'screener','stock-workbench:600519.SH'}
    after=state(factory)
    assert {k:v for k,v in before['pointers'].items() if k not in report['products']}=={k:v for k,v in after['pointers'].items() if k not in report['products']}
    again=rebuild_stock_research(factory,dry_run=False)
    assert again['status']=='no_change' and again['promotedProducts']==[] and again['version']==report['version']

def test_company_projection_without_new_holders_preserves_published_holdings():
    from app.stocks.marts.stock_research_runtime import project_research_snapshots
    from app.marts.contracts import MartCandidate,MartRows
    from datetime import datetime,timezone
    current={'screener':{'stocks':[{'symbol':'600519.SH'}]},'stock-workbench:600519.SH':{'quote':{},'metrics':{},'financial':{'statements':{},'dashboard':{},'shareholders':{'holders':[{'name':'old','ratio':10}],'periods':[{'reportDate':'2025-12-31'}]}}}}
    payload={'quote':{},'metrics':{'fields':[],'years':[],'presets':[]},'financial':{'years':[],'reportTypes':[],'announcementDate':'','statements':{'tables':[]},'dashboard':{'trends':[]},'shareholders':{'holders':[],'availability':{'dividendSeries':{'status':'missing','reason':'none'}}}}}
    at=datetime(2026,10,21,tzinfo=timezone.utc)
    screen=MartCandidate('screener',at,{'stocks':[{'symbol':'600519.SH'}],'bootstrap':{}},MartRows())
    work=MartCandidate('stock-workbench:600519.SH',at,payload,MartRows())
    result=project_research_snapshots(current,screen,[work])[1].payload
    assert result['financial']['shareholders']['holders']==current[work.product]['financial']['shareholders']['holders']
    assert result['financial']['shareholders']['periods']==current[work.product]['financial']['shareholders']['periods']


def test_busy_lock_or_missing_workbench_do_not_write(pg_factory, monkeypatch):
    import app.stocks.marts.stock_research_runtime as module
    factory=pg_factory;old_stock(factory);before=state(factory)
    held=ProcessLocalJobLock();assert held.try_acquire(data_publication_lock_key())
    monkeypatch.setattr(module,'job_lock_for_engine',lambda engine:held)
    assert rebuild_stock_research(factory,dry_run=False)['code']=='STOCK_PUBLICATION_LOCK_BUSY'
    assert state(factory)==before
    held.release(data_publication_lock_key())
    with factory.begin() as session:session.delete(session.get(PublishedSnapshot,'stock-workbench:600519.SH'))
    before=state(factory)
    assert rebuild_stock_research(factory,dry_run=False)['code']=='STOCK_BASE_DATE_OR_SCOPE_MISMATCH'
    assert state(factory)==before

def test_cli_requires_publish_and_dry_run_wins(monkeypatch):
    from app.cli.main import app
    import app.cli.stock_research as module
    assert_fixture_configuration_rejected(monkeypatch)
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls:Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test')))
    class Engine:
        def dispose(self):pass
    monkeypatch.setattr(module,'create_engine_and_session',lambda url:(Engine(),'test factory'))
    calls=[]
    def run(factory,*,dry_run):
        assert factory=='test factory';calls.append(dry_run)
        return {'status':'planned' if dry_run else 'completed'}
    monkeypatch.setattr(module,'rebuild_stock_research',run)
    for args in [[],['--dry-run'],['--publish'],['--publish','--dry-run']]:assert CliRunner().invoke(app,['rebuild-stock-research',*args]).exit_code==0
    assert calls==[True,True,False,True]
def test_business_only_rebuild_preserves_unrelated_workbench_and_screener(pg_factory, monkeypatch):
    from app.stocks import business_projection
    factory = pg_factory
    old_stock(factory)
    product = 'stock-workbench:600519.SH'
    def read_pair():
        with factory() as session:
            return [deepcopy(session.get(PublishedSnapshot, k).payload) for k in (product, 'screener')]
    before, screen = read_pair()
    history = {'compositionVersion': 2, 'reports': [], 'historyComplete': False,
               'visibility': 'report-period-only', 'sourceBaseVersion': 'test',
               'sourceAsOf': '2026-10-21', 'capturedAt': '2026-11-06T00:00:00Z'}
    monkeypatch.setattr(business_projection, 'load_business_histories', lambda *args: {'600519.SH': history})
    report = rebuild_stock_research(factory, dry_run=False, business_only=True)
    assert report['promotedProducts'] == [product]
    after, same_screen = read_pair()
    before['financial']['dashboard']['businessHistory'] = history
    assert after == before and same_screen == screen
    assert rebuild_stock_research(factory, dry_run=False, business_only=True)['status'] == 'no_change'
