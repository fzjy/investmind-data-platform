"""真实仓库包与三个消费入口的缺失状态契约；不请求供应商。"""
from copy import deepcopy
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.db.models import PublishedSnapshot
from app.stocks.derived.inputs import CoreSnapshotRepository
from app.market.data.catalog import MarketDataCatalog
from app.market.commodities.publication import publish_commodity_marts
from app.market.commodities.repository import package
from app.market.commodities.missing import reconcile_commodity_missing
from app.market.marts.market import MarketMartBuilder
from app.market.marts.market_runtime import rebuild_market_marts
from backend.tests.market_commodities.test_sources import complete
from backend.tests.market_commodities.test_sync import setup, NOW, DAY, POOL
from backend.tests.marts.test_content_runtime import state

GENERIC = {'source': 'commodities', 'reason': '尚未采集真实来源'}
FOREIGN = {'source': 'commodities:overseas', 'reason': '海外商品真实来源未接入'}
INTRADAY = {'source': 'commodities:intraday', 'reason': '日内商品真实来源未接入'}
OTHER = {'source': 'fundFlow', 'reason': 'SOURCE_PERMISSION_REQUIRED', 'code': 'OLD_FLOW_FAULT'}
FAULT = {'source': 'commodities', 'reason': 'SOURCE_TIMEOUT', 'code': 'OLD_COMMODITY_FAULT'}
EXPECTED = [OTHER, FAULT, FOREIGN, INTRADAY]


def seed_missing(factory):
    with factory.begin() as session:
        for pointer in session.scalars(select(PublishedSnapshot).where(
            PublishedSnapshot.product.in_(('market-overview', 'market-explorer')))):
            pointer.payload = {**pointer.payload, 'missingData': [OTHER, GENERIC, FAULT]}


def test_direct_publication_replaces_only_generic_marker_in_both_products(new_pg_factory):
    sync, factory, _, calls = setup(new_pg_factory)
    complete(factory, sync.catalog)
    seed_missing(factory)
    before = state(factory)
    report = publish_commodity_marts(factory, POOL, sync.catalog, checked_at=NOW, expected_as_of=DAY)
    assert report['status'] == 'completed' and calls == []
    after = state(factory)
    for product in ('market-overview', 'market-explorer'):
        assert after['pointers'][product][2]['missingData'] == EXPECTED
    overview_before = deepcopy(before['pointers']['market-overview'][2])
    overview_after = deepcopy(after['pointers']['market-overview'][2])
    overview_before.pop('missingData'); overview_after.pop('missingData')
    assert overview_after == overview_before
    assert after['pointers']['macro-insights'] == before['pointers']['macro-insights']
    assert publish_commodity_marts(factory, POOL, sync.catalog, checked_at=NOW,
        expected_as_of=DAY)['status'] == 'no_change'
    assert state(factory) == after and calls == []


def test_builder_reports_domestic_resolved_and_remaining_subscopes(new_pg_factory):
    sync, factory, _, _ = setup(new_pg_factory)
    complete(factory, sync.catalog)
    with factory() as session:
        verified = package(session, sync.catalog, DAY, NOW)
    snapshot = CoreSnapshotRepository(factory).load(DAY, POOL.symbols)
    candidates = MarketMartBuilder(commodity_package=verified).build(snapshot, 'controlled')
    for candidate in candidates:
        assert GENERIC not in candidate.payload['missingData']
        assert FOREIGN in candidate.payload['missingData']
        assert INTRADAY in candidate.payload['missingData']
        assert tuple(candidate.payload['missingData']) == candidate.missing_data


def test_rebuild_cannot_reintroduce_generic_marker_or_drop_concrete_fault(new_pg_factory):
    sync, factory, _, calls = setup(new_pg_factory)
    complete(factory, sync.catalog)
    seed_missing(factory)
    with patch.object(MarketDataCatalog, 'load', return_value=sync.catalog):
        assert rebuild_market_marts(factory, POOL, checked_at=NOW, dry_run=False)['status'] == 'completed'
        after = state(factory)
        for product in ('market-overview', 'market-explorer'):
            missing = after['pointers'][product][2]['missingData']
            assert GENERIC not in missing
            assert all(row in missing for row in EXPECTED)
        assert rebuild_market_marts(factory, POOL, checked_at=NOW, dry_run=False)['status'] == 'no_change'
        assert state(factory) == after and calls == []


@pytest.mark.parametrize('damage', ['none', 'empty', 'count', 'bar', 'identity'])
def test_unverified_or_invalid_package_never_clears_missing_or_adds_success(new_pg_factory, damage):
    sync, factory, _, _ = setup(new_pg_factory)
    complete(factory, sync.catalog)
    with factory() as session:
        verified = package(session, sync.catalog, DAY, NOW)
    if damage == 'none': verified = None
    elif damage == 'empty': verified = {}
    elif damage == 'count': verified['commodityMetadata']['instrumentCount'] = 9
    elif damage == 'bar': verified['commodityData']['precious']['domestic'][0]['bars']['daily'] = []
    else: verified['commodityData']['precious']['domestic'][0]['code'] = 'NOT_REAL'
    original = [OTHER, GENERIC, FAULT]
    before = deepcopy(original)
    assert reconcile_commodity_missing(original, verified) == tuple(before)
    assert original == before


def test_specific_same_reason_fault_and_existing_subscope_fault_are_preserved(new_pg_factory):
    sync, factory, _, _ = setup(new_pg_factory)
    complete(factory, sync.catalog)
    with factory() as session:
        verified = package(session, sync.catalog, DAY, NOW)
    coded = {**GENERIC, 'code': 'OLD_PROVIDER_FAILURE'}
    overseas_fault = {'source': 'commodities:overseas', 'reason': 'PERMISSION_REQUIRED'}
    original = [GENERIC, coded, overseas_fault, OTHER]
    before = deepcopy(original)
    result = reconcile_commodity_missing(original, verified)
    assert result == (coded, overseas_fault, OTHER, INTRADAY)
    assert original == before
    assert reconcile_commodity_missing(result, verified) == result
