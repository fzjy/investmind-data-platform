from datetime import datetime

from app.marts.contracts import MartCandidate, MartRows
from app.marts.quality import QualityContext, QualityGate


def test_quality_gate_collects_blocking_and_advisory_results() -> None:
    candidate = MartCandidate(
        "screener",
        __import__("datetime").datetime(2026, 8, 24),
        {"bootstrap": {}, "stocks": []},
    )
    context = QualityContext(2, (), 0, 0, ("macro",))

    results = QualityGate().evaluate((candidate,), context)
    by_name = {result.name: result for result in results}

    assert by_name["screener-not-empty"].severity == "blocking"
    assert by_name["screener-not-empty"].passed is False
    assert by_name["external-data-coverage"].severity == "advisory"


def test_quality_gate_blocks_empty_requested_workbench_and_duplicate_rows() -> None:
    workbench = MartCandidate(
        "stock-workbench:600519.SH",
        datetime(2026, 8, 24),
        {
            "symbol": "600519.SH",
            "sidebar": {"stocks": []},
            "quote": {"price": None, "intraday": {"day": [], "fiveDay": []}},
            "dailyChart": {"candles": []},
            "metrics": {"cards": [], "fields": []},
            "risks": {"groups": []},
            "financial": {"dashboard": {"products": []}, "shareholders": {"holders": []}, "statements": {"tables": []}},
            "isFallbackSample": False,
            "dataAvailability": {},
        },
        MartRows(
            workbenches=({"symbol": "600519.SH"},),
            charts=(),
        ),
    )
    screener = MartCandidate(
        "screener",
        datetime(2026, 8, 24),
        {
            "bootstrap": {"groups": [], "thresholds": [], "categoryValues": {}},
            "stocks": [
                {"symbol": "600519.SH", "price": 1500},
                {"symbol": "600519.SH", "price": 1500},
            ],
        },
        MartRows(screener=({"symbol": "600519.SH"}, {"symbol": "600519.SH"})),
    )
    results = QualityGate().evaluate(
        (screener, workbench),
        QualityContext(1, ("600519.SH",), 1, 1, ()),
    )
    by_name = {result.name: result for result in results}

    assert by_name["workbench-data"].passed is False
    assert by_name["logical-keys-unique"].passed is False


def test_quality_gate_blocks_empty_portfolio_and_market_breadth() -> None:
    portfolio = MartCandidate(
        "portfolio",
        datetime(2026, 8, 24),
        {"assets": [{"id": "CNY"}], "prices": {"CNY": []}, "defaultState": {"positions": []}, "snapshotLabel": "2026-08-24", "storageKey": "x"},
    )
    market = MartCandidate(
        "market-overview",
        datetime(2026, 8, 24),
        {
            "marketSnapshot": {"asOf": "2026-08-24"},
            "marketStatuses": [], "globalIndices": [],
            "turnoverSnapshot": {"value": "--"}, "turnoverTop10": [],
            "limitStats": {"riseLimit": 0},
            "sentimentSnapshot": {"score": 0}, "fundFlowSnapshot": {},
            "breadthSnapshots": [], "etfQuotes": [],
        },
    )
    results = QualityGate().evaluate(
        (portfolio, market),
        QualityContext(1, (), 1, 1, ()),
    )
    by_name = {result.name: result for result in results}

    assert by_name["portfolio-has-assets"].passed is False
    assert by_name["market-breadth-not-empty"].passed is False


def test_quality_gate_blocks_future_announcement_and_as_of_dates() -> None:
    candidate = MartCandidate(
        "custom-product",
        datetime(2026, 8, 24),
        {
            "announcementDate": "2026-08-25",
            "nested": {"asOf": "2026-08-26T00:00:00+00:00"},
        },
    )

    results = QualityGate().evaluate(
        (candidate,),
        QualityContext(0, (), 0, 0, ()),
    )
    result = next(item for item in results if item.name == "no-future-data")

    assert result.passed is False
    assert result.details["dates"] == {
        "custom-product": ["2026-08-25", "2026-08-26T00:00:00+00:00"]
    }


def test_quality_gate_blocks_duplicate_source_logical_keys() -> None:
    results = QualityGate().evaluate(
        (),
        QualityContext(
            0,
            (),
            0,
            0,
            (),
            source_duplicate_keys=("core.daily_price:600519.SH:2026-08-24",),
        ),
    )
    result = next(
        item for item in results if item.name == "source-logical-keys-unique"
    )

    assert result.severity == "blocking"
    assert result.passed is False
