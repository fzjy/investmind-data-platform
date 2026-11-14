from copy import deepcopy
from dataclasses import dataclass

import pytest

from app.shared.contracts import DataMeta, ProviderSnapshot
from app.stocks.definitions import THRESHOLD_DEFINITIONS
from app.stocks.models import ScreenerRequest
from app.stocks.normalizer import normalize_screener_universe
from app.stocks.service import ScreenerService


@dataclass
class StoredPayloadProvider:
    snapshot: ProviderSnapshot

    def read_screener_universe(self):
        return self.snapshot


LEGACY_ROWS = [
    {"symbol": "600519.SH", "board": "主板", "roe": None},
    {"symbol": "000001.SZ", "board": "主板", "roe": None},
    {"symbol": "688111.SH", "board": "科创板", "roe": None},
    {"symbol": "300750.SZ", "board": "创业板", "roe": None},
    {"symbol": "920002.BJ", "board": "北交所", "roe": None},
]


def request_for(board):
    return ScreenerRequest.model_validate({
        "filters": {"preset": None,
            "thresholds": {key: definition.default for key, definition in THRESHOLD_DEFINITIONS.items()},
            "enabledThresholds": [], "exclusions": [], "boards": [board] if board else ["sh-main", "sz-main", "star", "chinext", "bse"],
            "categories": {"majorIndustry": [], "subIndustry": [], "concept": [], "region": []}},
        "sortRules": [], "page": 1, "pageSize": 200,
    })


@pytest.mark.parametrize("board,symbol", [
    ("sh-main", "600519.SH"), ("sz-main", "000001.SZ"),
    ("star", "688111.SH"), ("chinext", "300750.SZ"), ("bse", "920002.BJ"),
])
def test_query_filters_legacy_board_without_republishing_or_mutating_source(board, symbol):
    raw = {"bootstrap": {"totalCount": 5}, "stocks": deepcopy(LEGACY_ROWS)}
    original = deepcopy(raw)
    meta = DataMeta(source="database", asOf="2026-09-18T00:00:00Z", version="old-v1", stale=True)
    service = ScreenerService(StoredPayloadProvider(ProviderSnapshot(raw, meta)))
    result = service.query(request_for(board))
    assert result.data["total"] == 1
    assert result.data["items"][0]["symbol"] == symbol
    assert result.data["items"][0]["board"] == board
    assert result.data["items"][0]["roe"] is None
    assert result.meta == meta
    assert raw == original
    assert service.query(request_for(None)).data["total"] == 5


@pytest.mark.parametrize("board,symbol", [
    ("sh-main", "600519.SH"), ("sz-main", "000001.SZ"),
    ("star", "688111.SH"), ("chinext", "300750.SZ"), ("bse", "920002.BJ"),
    ("未识别板块", "600519.SH"), ("主板", "１２３４５６.SH"), ("主板", "invalid.SH"),
])
def test_normalization_preserves_canonical_or_unresolved_board(board, symbol):
    raw = {"bootstrap": {}, "stocks": [{"symbol": symbol, "board": board}]}
    assert normalize_screener_universe(raw)["stocks"][0]["board"] == board


@pytest.mark.parametrize("exchange,symbol", [("SSE", "688111.SH"), ("SZSE", "300750.SZ")])
@pytest.mark.parametrize("board", [None, "", "未识别板块"])
def test_exchange_alone_does_not_turn_unknown_board_into_main(exchange, symbol, board):
    raw = {"bootstrap": {}, "stocks": [{"symbol": symbol, "board": board, "exchange": exchange}]}
    result = normalize_screener_universe(raw)
    assert result["stocks"][0]["board"] == board
    service = ScreenerService(StoredPayloadProvider(ProviderSnapshot(raw,
        DataMeta(source="database", asOf="2026-09-18", version="v1", stale=False))))
    for selection in ("sh-main", "sz-main"):
        assert service.query(request_for(selection)).data["total"] == 0
