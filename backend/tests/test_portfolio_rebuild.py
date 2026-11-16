from backend.tests.support.snapshots import set_snapshot_version
from copy import deepcopy
from datetime import date, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from typer.testing import CliRunner

from app.db.models import AdjustmentFactor, DailyPrice, PublishedSnapshot, Security
from app.jobs.locks import ProcessLocalJobLock, data_publication_lock_key

from backend.tests.marts.test_content_runtime import prepare, state


def runtime():
    from app.portfolio.marts.portfolio_runtime import rebuild_portfolio
    return rebuild_portfolio


def source(new_pg_factory):
    factory = new_pg_factory()
    prepare(factory)
    with factory.begin() as session:
        session.add(AdjustmentFactor(ts_code='600519.SH', trade_date=date(2026,8,24), adj_factor=Decimal('1')))
        p = session.get(PublishedSnapshot, 'portfolio')
        payload = deepcopy(p.payload)
        payload.pop('unadjustedPrices', None)
        p.payload = payload
    return factory


def test_default_preview_writes_nothing_and_uses_published_scope(new_pg_factory):
    factory = source(new_pg_factory)
    with factory.begin() as s:
        s.add(Security(ts_code='OTHER',symbol='OTHER',name='外部证券'))
        s.flush()
        s.add(DailyPrice(ts_code='OTHER',trade_date=date(2026,8,24),close=Decimal('1')))
    before = state(factory)
    report = runtime()(factory)
    assert report['status'] == 'planned' and report['wouldPublish']
    assert report['symbolCount'] == 1 and report['requestCount'] == 0
    assert report['asOf'] == '2026-08-24' and state(factory) == before


def test_publish_changes_only_portfolio_then_reentry_is_no_change(new_pg_factory):
    factory = source(new_pg_factory)
    before = state(factory)
    report = runtime()(factory, dry_run=False)
    assert report['status'] == 'completed' and report['promotedProducts'] == ['portfolio']
    after = state(factory)
    for key, value in before['pointers'].items():
        if key != 'portfolio': assert after['pointers'][key] == value
    assert after['pages'][:-1] == before['pages']
    payload = after['pointers']['portfolio'][2]
    assert payload['unadjustedPrices']['600519.SH'][0]['price'] == 1500
    assert payload['assets'][0]['listDate'] == '2001-08-27'
    again = runtime()(factory, dry_run=False)
    assert again['status'] == 'no_change' and again['promotedProducts'] == []
    assert state(factory) == after


@pytest.mark.parametrize('value,reason', [(None, 'missing'), (0, 'invalid')])
def test_factor_issue_can_be_published_then_repaired(new_pg_factory, value, reason):
    factory = source(new_pg_factory)
    with factory.begin() as s:
        row = s.scalar(select(AdjustmentFactor))
        if value is None:
            s.delete(row)
        else:
            row.adj_factor = value
    assert runtime()(factory, dry_run=False)['status'] == 'completed'
    with factory.begin() as s:
        coverage = s.get(PublishedSnapshot, 'portfolio').payload['factorCoverage']['600519.SH']
        assert coverage['issues'] == [{'date': '2026-08-24', 'reason': reason}]
        assert not coverage['baseValid']
        row = s.scalar(select(AdjustmentFactor))
        if row is None:
            s.add(AdjustmentFactor(ts_code='600519.SH', trade_date=date(2026,8,24), adj_factor=1))
        else:
            row.adj_factor = 1
    assert runtime()(factory, dry_run=False)['status'] == 'completed'
    with factory() as s:
        assert s.get(PublishedSnapshot, 'portfolio').payload['factorCoverage']['600519.SH']['issues'] == []


@pytest.mark.parametrize('bad', ['missing-price', 'zero-price', 'empty-scope', 'duplicate-scope', 'newer-portfolio'])
def test_bad_source_never_publishes(new_pg_factory, bad):
    factory = source(new_pg_factory)
    with factory.begin() as s:
        if bad == 'missing-price': s.execute(delete(DailyPrice))
        if bad == 'zero-price': s.scalar(select(DailyPrice)).close = 0
        if bad in {'empty-scope','duplicate-scope'}:
            p=s.get(PublishedSnapshot,'screener'); d=deepcopy(p.payload)
            d['stocks'] = [] if bad == 'empty-scope' else d['stocks'] * 2
            p.payload=d
        if bad == 'newer-portfolio': s.get(PublishedSnapshot,'portfolio').as_of = s.get(PublishedSnapshot,'portfolio').as_of.replace(year=2027)
    before=state(factory)
    assert runtime()(factory,dry_run=False)['status'] == 'blocked'
    assert state(factory) == before


def test_lock_conflict_and_parent_change_protect_user_work(new_pg_factory, monkeypatch):
    import app.portfolio.marts.portfolio_runtime as module
    factory=source(new_pg_factory);before=state(factory)
    held=ProcessLocalJobLock();assert held.try_acquire(data_publication_lock_key())
    monkeypatch.setattr(module,'job_lock_for_engine',lambda engine:held)
    try: assert runtime()(factory,dry_run=False)['code']=='PORTFOLIO_PUBLICATION_LOCK_BUSY'
    finally: held.release(data_publication_lock_key())
    assert state(factory)==before
    original=module.PortfolioMartBuilder.build
    def intervening(self,*args,**kwargs):
        candidate=original(self,*args,**kwargs)
        with factory.begin() as s: set_snapshot_version(s, s.get(PublishedSnapshot,'portfolio'), 'user-new-version')
        return candidate
    monkeypatch.setattr(module.PortfolioMartBuilder,'build',intervening)
    report=runtime()(factory,dry_run=False)
    assert report['code']=='PORTFOLIO_BASE_CHANGED'
    with factory() as s: assert s.get(PublishedSnapshot,'portfolio').version=='user-new-version'


def test_cli_explicit_publish_and_dry_run_priority(monkeypatch):
    from app.cli.main import app
    from app.config import Settings
    import app.cli.portfolio_rebuild as module
    monkeypatch.setattr(Settings,'from_env',classmethod(lambda cls:Settings(data_backend='database',database_url='postgresql+psycopg://controlled.invalid/test')))
    class Engine:
        def dispose(self): pass
    monkeypatch.setattr(module,'create_engine_and_session',lambda url:(Engine(),'factory'))
    calls=[]
    monkeypatch.setattr(module,'rebuild_portfolio',lambda factory,*,dry_run:(calls.append(dry_run) or {'status':'planned'}))
    for args in [[],['--dry-run'],['--publish'],['--publish','--dry-run']]:
        assert CliRunner().invoke(app,['rebuild-portfolio',*args]).exit_code==0
    assert calls==[True,True,False,True]


def test_rebuild_excludes_project_settings_and_protects_price_history(new_pg_factory):
    factory=source(new_pg_factory)
    with factory.begin() as s:
        p=s.get(PublishedSnapshot,'portfolio');d=deepcopy(p.payload)
        d['defaultState']={'positions':[{'assetId':'600519.SH','weight':100}]}
        d['storageKey']='user-owned-key';p.payload=d
    report=runtime()(factory,dry_run=False)
    assert report['status']=='completed'
    with factory() as s:
        payload=s.get(PublishedSnapshot,'portfolio').payload
        assert 'defaultState' not in payload and 'storageKey' not in payload
    with factory.begin() as s:
        p=s.get(PublishedSnapshot,'portfolio');d=deepcopy(p.payload)
        d['prices']['600519.SH'].insert(0,{'date':'2026-08-21','price':1400})
        p.payload=d
    before=state(factory)
    assert runtime()(factory,dry_run=False)['code']=='PORTFOLIO_HISTORY_REGRESSION'
    assert state(factory)==before


def test_atomic_publisher_guard_rejects_race_without_writing_history(new_pg_factory, monkeypatch):
    import app.portfolio.marts.portfolio_runtime as module
    factory=source(new_pg_factory);original=module.SnapshotPublisher.publish_many
    before=state(factory)
    def racing(self,*args,**kwargs):
        with factory.begin() as s:
            pointer = s.get(PublishedSnapshot, 'screener')
            pointer.payload = dict(pointer.payload)
            pointer.version = 'racing-user-version'
        return original(self,*args,**kwargs)
    monkeypatch.setattr(module.SnapshotPublisher,'publish_many',racing)
    report=runtime()(factory,dry_run=False)
    assert report['code']=='PORTFOLIO_BASE_CHANGED'
    after=state(factory)
    assert after['pages']==before['pages'] and after['pointers']['portfolio']==before['pointers']['portfolio']


def test_missing_target_day_quote_is_not_relabeled_as_fresh(new_pg_factory):
    factory=source(new_pg_factory)
    with factory.begin() as s:
        s.scalar(select(DailyPrice)).trade_date=date(2026,8,21)
        s.scalar(select(AdjustmentFactor)).trade_date=date(2026,8,21)
        p=s.get(PublishedSnapshot,'portfolio');d=deepcopy(p.payload)
        d['prices']['600519.SH'][0]['date']='2026-08-21';p.payload=d
        p.as_of=p.as_of.replace(day=21)
    before=state(factory)
    assert runtime()(factory,dry_run=False)['code']=='PORTFOLIO_TARGET_QUOTE_MISSING'
    assert state(factory)==before


def test_atomic_guard_preserves_concurrent_freshness_without_version_change(new_pg_factory, monkeypatch):
    import app.portfolio.marts.portfolio_runtime as module
    factory=source(new_pg_factory);original=module.SnapshotPublisher.publish_many
    def refreshing(self,*args,**kwargs):
        with factory.begin() as s:
            p=s.get(PublishedSnapshot,'portfolio');p.stale=True;p.stale_reason='new-check-failed'
        return original(self,*args,**kwargs)
    monkeypatch.setattr(module.SnapshotPublisher,'publish_many',refreshing)
    before=state(factory)
    assert runtime()(factory,dry_run=False)['code']=='PORTFOLIO_BASE_CHANGED'
    after=state(factory)
    assert after['pages']==before['pages'] and after['pointers']['portfolio'][3:5]==(True,'new-check-failed')


def test_session_timezone_does_not_change_business_date(new_pg_factory, monkeypatch):
    from sqlalchemy.orm import Session
    factory=source(new_pg_factory);original=Session.get
    def west(self,entity,ident,*args,**kwargs):
        row=original(self,entity,ident,*args,**kwargs)
        if entity is PublishedSnapshot and row is not None:
            dt=row.as_of.replace(tzinfo=timezone.utc) if row.as_of.tzinfo is None else row.as_of
            row.as_of=dt.astimezone(timezone(timedelta(hours=-7)))
        return row
    monkeypatch.setattr(Session,'get',west)
    report=runtime()(factory)
    assert report['status']=='planned' and report['asOf']=='2026-08-24'
