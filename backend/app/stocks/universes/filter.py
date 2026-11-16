from __future__ import annotations

from dataclasses import dataclass

from app.connectors.tushare.contracts import Scalar, TushareResponse
from app.connectors.tushare.strategies import Dataset
from app.stocks.universes.models import UniverseSelection
from app.stocks.universes.resolver import STOCK_SYMBOL_PATTERN


UNFILTERED_DATASETS = frozenset(
    {
        Dataset.SECURITY,
        Dataset.TRADE_CALENDAR,
        Dataset.CLASSIFICATION,
    }
)


@dataclass(frozen=True, slots=True)
class FilteredResponse:
    response: TushareResponse
    provider_count: int
    retained_count: int
    filtered_count: int


class UniverseRowFilter:
    def __init__(self, selection: UniverseSelection) -> None:
        self._selection = selection
        self._allowed = frozenset(selection.symbols)

    def filter(
        self,
        dataset: Dataset,
        response: TushareResponse,
    ) -> FilteredResponse:
        provider_count = len(response.items)
        if (
            self._selection.coverage == "full-market"
            or dataset in UNFILTERED_DATASETS
            or "ts_code" not in response.fields
        ):
            return FilteredResponse(
                response=response,
                provider_count=provider_count,
                retained_count=provider_count,
                filtered_count=0,
            )

        symbol_index = response.fields.index("ts_code")
        retained: list[tuple[Scalar, ...]] = []
        for item in response.items:
            value = item[symbol_index]
            if not isinstance(value, str):
                retained.append(item)
                continue
            symbol = value.strip().upper()
            if not STOCK_SYMBOL_PATTERN.fullmatch(symbol) or symbol in self._allowed:
                retained.append(item)

        filtered_count = provider_count - len(retained)
        if filtered_count == 0:
            filtered_response = response
        else:
            filtered_response = TushareResponse(
                fields=response.fields,
                items=tuple(retained),
                has_more=response.has_more,
            )
        return FilteredResponse(
            response=filtered_response,
            provider_count=provider_count,
            retained_count=len(retained),
            filtered_count=filtered_count,
        )
