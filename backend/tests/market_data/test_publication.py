import copy
import importlib
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.models import PublishedSnapshot
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.marts.publication import SnapshotPublisher
from backend.tests.market_data.test_catalog import catalog
from backend.tests.market_data.test_repository import NOW, setup
from backend.tests.market_data.test_sync import job
from backend.tests.marts.test_content_runtime import prepare, state
from backend.tests.marts.test_market_runtime import SELECTION


def prepared(new_pg_factory, count=15):
    value, factory, _ = setup(new_pg_factory)
    prepare(factory)
    sync, client, _ = job(factory)
    try:
        sync.run(NOW)
    finally:
        client.close()
    if count == 14:
        with factory.begin() as session:
            session.query(value.MarketDailyQuote).filter_by(asset_code='IXIC').delete()
    return factory


def publish(factory):
    assert importlib.util.find_spec('app.market.data.publication') is not None, 'market quote publication missing'
    return importlib.import_module('app.market.data.publication').publish_market_quotes(factory, catalog(), checked_at=NOW)


def test_asset_dates_newer_than_stock_day_do_not_clear_stock_stale(new_pg_factory):
    factory = prepared(new_pg_factory)
    with factory.begin() as session:
        pointer = session.get(PublishedSnapshot, 'market-overview')
        pointer.stale, pointer.stale_reason = True, 'SNAPSHOT_BEHIND_EXPECTED_DATE'
    before = state(factory)
    result = publish(factory)
    assert result['status'] == 'completed' and len(result['promotedProducts']) == 2
    after = state(factory)
    overview = after['pointers']['market-overview']
    assert overview[1] == before['pointers']['market-overview'][1]
    assert overview[3:5] == (True, 'SNAPSHOT_BEHIND_EXPECTED_DATE')
    payload = overview[2]
    assert len(payload['globalIndices']) == 6 and len(payload['etfQuotes']) == 9
    assert payload['globalIndices'][0]['tradeDate'] == '2026-10-15'
    assert payload['globalIndices'][4]['tradeDate'] == '2026-10-14'
    assert payload['globalIndices'][0]['intraday'] is None
    for product, original in before['pointers'].items():
        if product not in {'market-overview','market-explorer'}:
            assert after['pointers'][product] == original
    for product in ('market-overview', 'market-explorer'):
        actual = after['pointers'][product][2]
        original = before['pointers'][product][2]
        assert actual['missingData'] == [row for row in original['missingData']
            if row['source'] not in {'indices', 'etfs'}]
    assert {k: v for k, v in after['pointers']['market-explorer'][2].items() if k != 'missingData'} == {
        k: v for k, v in before['pointers']['market-explorer'][2].items() if k != 'missingData'}
    assert after['workbenches'] == before['workbenches']
    assert publish(factory)['status'] == 'no_change'
    assert state(factory) == after


def test_fourteen_assets_never_promote_either_market_pointer(new_pg_factory):
    factory = prepared(new_pg_factory, 14)
    before = state(factory)
    report = publish(factory)
    assert report['status'] == 'blocked' and report['promotedProducts'] == []
    assert state(factory) == before


def test_second_market_write_failure_rolls_back_pair_and_history(new_pg_factory, monkeypatch):
    factory = prepared(new_pg_factory)
    before = state(factory)
    original = SnapshotPublisher._write_page_snapshot
    def fail(self, session, candidate, version):
        if candidate.product == 'market-explorer':
            raise RuntimeError('controlled second write')
        return original(self, session, candidate, version)
    monkeypatch.setattr(SnapshotPublisher, '_write_page_snapshot', fail)
    with pytest.raises(RuntimeError, match='controlled second write'):
        publish(factory)
    assert state(factory) == before


def test_shared_lock_does_not_write_and_newer_base_quote_cannot_regress(new_pg_factory):
    factory = prepared(new_pg_factory)
    lock = PostgresAdvisoryJobLock(factory.kw["bind"])
    assert lock.try_acquire(data_publication_lock_key())
    try:
        before = state(factory)
        assert publish(factory)['status'] == 'locked'
        assert state(factory) == before
    finally:
        lock.release(data_publication_lock_key())
    assert publish(factory)['status'] == 'completed'
    with factory.begin() as session:
        pointer = session.get(PublishedSnapshot, 'market-overview')
        payload = copy.deepcopy(pointer.payload)
        payload['globalIndices'][0]['tradeDate'] = '2026-10-16'
        pointer.payload = payload
    before = state(factory)
    assert publish(factory)['status'] == 'blocked'
    assert state(factory) == before


def test_stock_only_rebuild_preserves_valid_newer_asset_quotes(new_pg_factory):
    factory = prepared(new_pg_factory)
    publish(factory)
    before = state(factory)['pointers']['market-overview'][2]['globalIndices']
    from app.market.marts.market_runtime import rebuild_market_marts
    report = rebuild_market_marts(factory, SELECTION, checked_at=NOW, dry_run=False)
    assert report['status'] == 'completed', report
    assert state(factory)['pointers']['market-overview'][2]['globalIndices'] == before
    after = state(factory)
    for product in ('market-overview', 'market-explorer'):
        assert not {'indices', 'etfs'} & {row['source'] for row in after['pointers'][product][2]['missingData']}
    missing_check = next(check for check in report['quality'] if 'missingSources' in check['details'])
    assert not {'indices', 'etfs'} & set(missing_check['details']['missingSources'])


@pytest.mark.parametrize('broken_group', ['globalIndices', 'etfQuotes'])
def test_only_complete_real_quote_groups_clear_their_own_missing_source(new_pg_factory, broken_group):
    from app.market.data.quality import clear_resolved_quote_missing
    from app.marts.contracts import MartCandidate
    factory = prepared(new_pg_factory)
    publish(factory)
    payload = copy.deepcopy(state(factory)['pointers']['market-overview'][2])
    payload[broken_group] = payload[broken_group][:-1]
    missing = tuple({'source': source, 'reason': 'preserve'} for source in ('indices', 'etfs', 'fundFlow', 'commodities'))
    candidates = [MartCandidate('market-overview', NOW, {**payload, 'missingData': list(missing)}, missing_data=missing),
        MartCandidate('market-explorer', NOW, {'missingData': list(missing)}, missing_data=missing)]
    result = clear_resolved_quote_missing(candidates, catalog=catalog())
    expected = {'indices' if broken_group == 'globalIndices' else 'etfs', 'fundFlow', 'commodities'}
    for candidate in result:
        assert {row['source'] for row in candidate.payload['missingData']} == expected
        assert {row['source'] for row in candidate.missing_data} == expected
    assert candidates[0].payload['missingData'] == list(missing), 'inputs must remain immutable'


@pytest.mark.parametrize('field,bad', [('tradeDate','2026-10-17'), ('capturedAt',(NOW + timedelta(seconds=1)).isoformat()),
    ('source','unverified'), ('change',float('nan')), ('intraday',[1,2])])
def test_quote_quality_rejects_bad_dates_sources_or_fake_intraday(new_pg_factory, field, bad):
    factory = prepared(new_pg_factory)
    publish(factory)
    from app.marts.contracts import MartCandidate
    from app.market.data.quality import evaluate_market_quality
    with factory() as session:
        pointer = session.get(PublishedSnapshot, 'market-overview')
        payload = copy.deepcopy(pointer.payload)
        payload['globalIndices'][0][field] = bad
        candidate = MartCandidate(pointer.product, pointer.as_of, payload)
    checks = evaluate_market_quality([candidate], checked_at=NOW, catalog=catalog())
    assert any(not check.passed and check.severity == 'blocking' for check in checks)


def test_active_ingestion_blocks_publication_until_asset_set_is_stable(new_pg_factory):
    factory = prepared(new_pg_factory)
    before = state(factory)
    lock = PostgresAdvisoryJobLock(factory.kw["bind"])
    assert lock.try_acquire('sync-market-data')
    try:
        assert publish(factory)['status'] == 'locked'
        assert state(factory) == before
    finally:
        lock.release('sync-market-data')


def test_deep_quote_dto_rejects_bad_change_before_any_consumer(new_pg_factory):
    from app.marts.dto import validate_product_payload
    factory = prepared(new_pg_factory)
    publish(factory)
    with factory() as session:
        payload = copy.deepcopy(session.get(PublishedSnapshot,'market-overview').payload)
    payload['globalIndices'][0]['change'] = 'not-a-number'
    assert 'globalIndices[0].change' in validate_product_payload('market-overview',payload)
