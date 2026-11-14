from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from typing import Literal

QUOTE_FIELDS = ('ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'pct_chg')


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, populate_by_name=True,
        alias_generator=to_camel, hide_input_in_errors=True)


class MarketAsset(StrictModel):
    asset_type: Literal['indices', 'etfs']
    asset_code: str = Field(strict=True, min_length=1, max_length=32)
    name: str = Field(strict=True, min_length=1, max_length=64)
    market: Literal['cn', 'hk', 'us']
    api_name: Literal['index_daily', 'index_global', 'fund_daily']


@dataclass(frozen=True)
class MarketTarget:
    asset: MarketAsset
    start_date: date
    end_date: date
    identity: str
    pct_tolerance: Decimal = Decimal('0.02')

    @property
    def params(self) -> dict[str, str]:
        return {'ts_code': self.asset.asset_code, 'start_date': self.start_date.strftime('%Y%m%d'),
            'end_date': self.end_date.strftime('%Y%m%d')}


@dataclass(frozen=True)
class NormalizedMarketQuote:
    asset: MarketAsset
    trade_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    pre_close: Decimal
    pct_chg: Decimal


@dataclass(frozen=True)
class MarketSyncReport:
    status: str
    request_count: int = 0
    retained_count: int = 0
    completed_assets: tuple[str, ...] = ()
    pending_assets: tuple[str, ...] = ()
    error_codes: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {to_camel(key): list(value) if isinstance(value, tuple) else value
            for key, value in asdict(self).items()}
