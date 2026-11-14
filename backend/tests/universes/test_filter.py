from __future__ import annotations

from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.strategies import Dataset
from app.stocks.universes.filter import UniverseRowFilter
from app.stocks.universes.models import UniverseSelection


def selection(
    symbols: tuple[str, ...],
    *,
    coverage: str = "representative",
) -> UniverseSelection:
    return UniverseSelection(
        profile="representative-50" if coverage == "representative" else "all",
        version="v1",
        coverage=coverage,
        symbols=symbols,
        fingerprint="fingerprint",
    )


def test_stock_rows_keep_allowed_and_malformed_rows_but_filter_other_symbols() -> None:
    response = TushareResponse(
        fields=("ts_code", "trade_date", "close"),
        items=(
            ("600519.SH", "20260904", 10.0),
            ("000001.SZ", "20260904", 9.0),
            (None, "20260904", 8.0),
        ),
    )

    result = UniverseRowFilter(selection(("600519.SH",))).filter(
        Dataset.DAILY,
        response,
    )

    assert result.response.items == (
        ("600519.SH", "20260904", 10.0),
        (None, "20260904", 8.0),
    )
    assert result.provider_count == 3
    assert result.retained_count == 2
    assert result.filtered_count == 1


def test_metadata_datasets_bypass_filtering() -> None:
    response = TushareResponse(
        fields=("ts_code", "name"),
        items=(("600519.SH", "贵州茅台"), ("000001.SZ", "平安银行")),
    )

    for dataset in (
        Dataset.SECURITY,
        Dataset.TRADE_CALENDAR,
        Dataset.CLASSIFICATION,
    ):
        result = UniverseRowFilter(selection(("600519.SH",))).filter(
            dataset,
            response,
        )
        assert result.response is response
        assert result.provider_count == 2
        assert result.retained_count == 2
        assert result.filtered_count == 0


def test_missing_symbol_column_is_preserved_for_normalizer_rejection() -> None:
    response = TushareResponse(
        fields=("trade_date", "close"),
        items=(("20260904", 10.0),),
    )

    result = UniverseRowFilter(selection(("600519.SH",))).filter(
        Dataset.DAILY,
        response,
    )

    assert result.response is response
    assert result.retained_count == 1
    assert result.filtered_count == 0


def test_full_market_selection_is_an_identity_filter() -> None:
    response = TushareResponse(
        fields=("ts_code", "trade_date"),
        items=(("600519.SH", "20260904"), ("000001.SZ", "20260904")),
    )

    result = UniverseRowFilter(
        selection(("000001.SZ", "600519.SH"), coverage="full-market")
    ).filter(Dataset.DAILY, response)

    assert result.response is response
    assert result.filtered_count == 0
