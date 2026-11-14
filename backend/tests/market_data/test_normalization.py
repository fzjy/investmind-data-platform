"""捕获串位、伪零、非法价格及将ETF除息前收误替换的错误。"""
import importlib
from datetime import datetime
from decimal import Decimal

import pytest

from app.connectors.tushare.contracts import TushareResponse
from backend.tests.market_data.test_catalog import catalog, catalog_module

FIELDS = ('ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'pct_chg')


def target(etf=False):
    return catalog_module().plan_market_targets(catalog(), datetime.fromisoformat('2026-10-15T15:13:00+00:00'))[6 if etf else 0]


def normalize(items, *, etf=False, fields=FIELDS):
    value = target(etf)
    response = TushareResponse(fields=fields, items=tuple(tuple(r) for r in items))
    result = importlib.import_module('app.market.data.normalization').normalize_market_response(
        value, response, pct_tolerance=Decimal('0.02'))
    assert response.items == tuple(tuple(r) for r in items)
    return result


def test_normalizer_preserves_zero_and_sorts_deduplicated_rows():
    row = ['000001.SH', '20261015', 100, 100, 100, 100, 100, 0]
    older = ['000001.SH', '20261014', 100, 100, 100, 100, 100, 0]
    result = normalize([row, older, row])
    assert len(result) == 2 and result[1].trade_date.isoformat() == '2026-10-15'
    assert result[1].pct_chg == Decimal('0') and result[1].close == Decimal('100')


@pytest.mark.parametrize('index,bad', [(0, '000001.SZ'), (1, '20261016'), (1, '20260901'),
    (1, '2026-10-15'), (2, True), (3, 99), (4, 101), (5, 0), (5, 'NaN'),
    (5, 'Infinity'), (5, None), (6, -1), (7, 5)])
def test_normalizer_rejects_malformed_or_out_of_scope_rows(index, bad):
    row = ['000001.SH', '20261015', 100, 100, 100, 100, 100, 0]
    row[index] = bad
    with pytest.raises(ValueError):
        normalize([row])


def test_normalizer_rejects_conflicts_missing_and_duplicate_fields():
    row = ['000001.SH', '20261015', 100, 100, 100, 100, 100, 0]
    conflict = ['000001.SH', '20261015', 101, 101, 101, 101, 100, 1]
    with pytest.raises(ValueError):
        normalize([row, conflict])
    with pytest.raises(ValueError):
        normalize([row[:-1]], fields=FIELDS[:-1])
    with pytest.raises(ValueError):
        normalize([row], fields=FIELDS[:-1] + ('close',))
    with pytest.raises(ValueError):
        normalize([])


def test_etf_pre_close_is_not_replaced_by_previous_close():
    result = normalize([
        ['510050.SH', '20261014', 100, 100, 100, 100, 100, 0],
        ['510050.SH', '20261015', 100, 100, 100, 100, 99, 1.0101]], etf=True)
    assert result[1].pre_close == Decimal('99') and result[1].close == Decimal('100')
