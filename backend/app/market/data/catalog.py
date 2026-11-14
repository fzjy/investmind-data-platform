from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BeforeValidator, Field, PlainSerializer, field_validator, model_validator

from app.market.data.contracts import MarketAsset, MarketTarget, StrictModel
from app.market.commodities.contracts import CommodityOptions
from app.market.sentiment import SentimentOptions

DEFAULT_MARKET_CATALOG_FILE = Path(__file__).resolve().parents[3] / 'config' / 'market-data.json'
ASSET_IDENTITIES = (
    ('indices', '000001.SH', 'cn', 'index_daily'), ('indices', '399001.SZ', 'cn', 'index_daily'),
    ('indices', '399006.SZ', 'cn', 'index_daily'), ('indices', 'HSI', 'hk', 'index_global'),
    ('indices', 'SPX', 'us', 'index_global'), ('indices', 'IXIC', 'us', 'index_global'),
    *(('etfs', code, 'cn', 'fund_daily') for code in ('510050.SH', '510300.SH', '159338.SZ',
        '510500.SH', '512100.SH', '588000.SH', '588190.SH', '510880.SH', '512890.SH')),
)


def _wall_time(value: object) -> time:
    if isinstance(value, time) and value.tzinfo is None and value.second == value.microsecond == 0:
        return value
    if not isinstance(value, str) or re.fullmatch(r'[0-2][0-9]:[0-5][0-9]', value) is None:
        raise ValueError('MARKET_TIME_INVALID')
    return time.fromisoformat(value)


WallTime = Annotated[time, BeforeValidator(_wall_time),
    PlainSerializer(lambda value: value.strftime('%H:%M'), return_type=str, when_used='json')]


def _zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('MARKET_TIMEZONE_INVALID') from exc
    return value


class MarketSyncOptions(StrictModel):
    lookback_days: int = Field(default=14, strict=True, ge=1, le=31)
    max_requests_per_run: int = Field(default=20, strict=True, ge=1, le=100)
    max_retries: int = Field(default=0, strict=True, ge=0, le=0)
    timeout_seconds: int = Field(default=20, strict=True, ge=1, le=60)
    pct_change_tolerance_percent_points: Decimal = Field(default=Decimal('0.02'), ge=0, le=Decimal('0.1'), allow_inf_nan=False)

    @field_validator('pct_change_tolerance_percent_points', mode='before')
    @classmethod
    def reject_bool_tolerance(cls, value):
        if isinstance(value, bool):
            raise ValueError('MARKET_TOLERANCE_INVALID')
        return value


class MarketSchedule(StrictModel):
    timezone: str = Field(strict=True)
    close_time: WallTime
    publish_delay_minutes: int = Field(default=120, strict=True, ge=0, le=1440)
    _validate_zone = field_validator('timezone')(_zone)


class MarketDailySchedule(StrictModel):
    frequency: Literal['daily'] = 'daily'
    time: WallTime = time(19)
    timezone: str = Field(default='Asia/Shanghai', strict=True)
    catchup_on_start: bool = Field(default=True, strict=True)
    _validate_zone = field_validator('timezone')(_zone)


class MarketPulseOptions(StrictModel):
    enabled: bool = Field(default=True, strict=True)
    max_requests_per_run: int = Field(default=60, strict=True, ge=1, le=60)
    max_retries: int = Field(default=0, strict=True, ge=0, le=0)
    timeout_seconds: int = Field(default=20, strict=True, ge=1, le=60)


class MarketRegionOptions(MarketPulseOptions):
    refresh_interval_days: int = Field(default=30, strict=True, ge=1, le=365)


class EtfHeatmapOptions(StrictModel):
    enabled: bool = Field(default=False, strict=True)
    max_requests_per_run: int = Field(default=20, strict=True, ge=1, le=20)
    max_retries: int = Field(default=0, strict=True, ge=0, le=0)
    timeout_seconds: int = Field(default=20, strict=True, ge=1, le=60)


class MarketDataCatalog(StrictModel):
    sentiment: SentimentOptions = Field(default_factory=SentimentOptions)
    schema_version: int = Field(default=1, strict=True, ge=1, le=1)
    enabled: bool = Field(default=True, strict=True)
    sync: MarketSyncOptions
    markets: dict[str, MarketSchedule]
    assets: tuple[MarketAsset, ...]
    schedule: MarketDailySchedule
    pulse: MarketPulseOptions = Field(default_factory=MarketPulseOptions)
    region: MarketRegionOptions = Field(default_factory=MarketRegionOptions)
    etf_heatmap: EtfHeatmapOptions = Field(default_factory=EtfHeatmapOptions)
    commodities: CommodityOptions = Field(default_factory=CommodityOptions)

    @model_validator(mode='after')
    def check_finite_catalog(self):
        actual = [(a.asset_type, a.asset_code, a.market, a.api_name) for a in self.assets]
        if len(actual) != 15 or len(set(actual)) != 15 or set(actual) != set(ASSET_IDENTITIES):
            raise ValueError('MARKET_ASSET_WHITELIST_INVALID')
        if set(self.markets) != {'cn', 'hk', 'us'}:
            raise ValueError('MARKET_GROUP_INVALID')
        return self

    @classmethod
    def load(cls, path: Path | None = None) -> MarketDataCatalog:
        return cls.model_validate_json((path or DEFAULT_MARKET_CATALOG_FILE).read_text(encoding='utf-8'))

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(self.model_dump(mode='json', by_alias=True, exclude={'pulse', 'region', 'etf_heatmap', 'commodities', 'sentiment'}), sort_keys=True,
            ensure_ascii=False, separators=(',', ':'))
        return hashlib.sha256(canonical.encode()).hexdigest()


def plan_market_targets(catalog: MarketDataCatalog, checked_at: datetime) -> tuple[MarketTarget, ...]:
    if checked_at.utcoffset() is None:
        raise ValueError('MARKET_AWARE_CLOCK_REQUIRED')
    targets = []
    for asset in catalog.assets:
        schedule = catalog.markets[asset.market]
        zone = ZoneInfo(schedule.timezone)
        local = checked_at.astimezone(zone)
        day = local.date()
        # Delay is elapsed time, not wall-clock arithmetic through a DST transition.
        while checked_at.astimezone(UTC) < datetime.combine(day, schedule.close_time, zone).astimezone(UTC) + timedelta(minutes=schedule.publish_delay_minutes):
            day -= timedelta(days=1)
        start = day - timedelta(days=catalog.sync.lookback_days - 1)
        identity = hashlib.sha256(f'{catalog.fingerprint}:{asset.asset_type}:{asset.asset_code}:{start}:{day}'.encode()).hexdigest()
        targets.append(MarketTarget(asset, start, day, identity, catalog.sync.pct_change_tolerance_percent_points))
    return tuple(targets)


def resolve_market_targets(catalog, checked_at, targets=None, *, planned_at=None):
    current_targets = plan_market_targets(catalog, checked_at)
    if planned_at is not None and (planned_at.utcoffset() is None or planned_at > checked_at):
        raise ValueError('MARKET_TARGET_INVALID')
    expected = plan_market_targets(catalog, planned_at) if planned_at is not None else current_targets
    if targets is None:
        return expected
    by_code = {t.asset.asset_code:t for t in expected}
    current_by_code = {t.asset.asset_code:t for t in current_targets}
    if len(targets) != 15 or {t.asset.asset_code for t in targets} != set(by_code):
        raise ValueError('MARKET_TARGET_INVALID')
    for target in targets:
        current = by_code[target.asset.asset_code]
        identity = hashlib.sha256(f'{catalog.fingerprint}:{target.asset.asset_type}:{target.asset.asset_code}:{target.start_date}:{target.end_date}'.encode()).hexdigest()
        if (target.asset != current.asset or target.identity != identity
            or (target.end_date-target.start_date).days != catalog.sync.lookback_days-1
            or target.start_date != current.start_date or target.end_date != current.end_date
            or target.end_date > current_by_code[target.asset.asset_code].end_date
            or target.pct_tolerance != catalog.sync.pct_change_tolerance_percent_points):
            raise ValueError('MARKET_TARGET_INVALID')
    return tuple(targets)
