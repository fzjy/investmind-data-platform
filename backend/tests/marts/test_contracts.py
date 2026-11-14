from datetime import date, datetime, timezone

from app.marts.contracts import MartCandidate, MartRows, release_version


def candidate(product: str, payload: dict[str, object]) -> MartCandidate:
    return MartCandidate(
        product=product,
        as_of=datetime(2026, 8, 24, tzinfo=timezone.utc),
        payload=payload,
    )


def test_release_version_is_order_independent() -> None:
    first = (
        candidate("screener", {"stocks": [1]}),
        candidate("portfolio", {"assets": [2]}),
    )
    second = tuple(reversed(first))

    assert release_version(date(2026, 8, 24), first) == release_version(
        date(2026, 8, 24),
        second,
    )


def test_release_version_changes_when_ranking_order_changes() -> None:
    first = candidate(
        "market-explorer",
        {"rankingLeaders": [{"symbol": "600519.SH"}, {"symbol": "000001.SZ"}]},
    )
    second = candidate(
        "market-explorer",
        {"rankingLeaders": [{"symbol": "000001.SZ"}, {"symbol": "600519.SH"}]},
    )

    assert release_version(date(2026, 8, 24), (first,)) != release_version(
        date(2026, 8, 24),
        (second,),
    )


def test_candidate_keeps_structured_mart_rows() -> None:
    rows = MartRows(screener=({"symbol": "600519.SH"},))
    result = MartCandidate(
        "screener",
        datetime(2026, 8, 24, tzinfo=timezone.utc),
        {"bootstrap": {}, "stocks": []},
        rows,
    )

    assert result.rows.screener[0]["symbol"] == "600519.SH"
    assert result.stale is False
    assert result.stale_reason is None
