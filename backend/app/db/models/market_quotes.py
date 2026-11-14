from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MarketDailyQuote(Base):
    __tablename__ = 'market_daily_quote'
    __table_args__ = (
        UniqueConstraint('provider', 'asset_type', 'asset_code', 'trade_date', name='uq_market_daily_quote_identity'),
        CheckConstraint("asset_type IN ('indices','etfs')", name='ck_market_quote_asset_type'),
        CheckConstraint('open > 0 AND high > 0 AND low > 0 AND close > 0 AND pre_close > 0 '
            'AND open < 1000000000000 AND high < 1000000000000 AND low < 1000000000000 '
            'AND close < 1000000000000 AND pre_close < 1000000000000 '
            'AND pct_chg > -1000000000000 AND pct_chg < 1000000000000', name='ck_market_quote_finite'),
        CheckConstraint('low <= open AND open <= high AND low <= close AND close <= high', name='ck_market_quote_ohlc'),
        {'schema': 'core'},
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    api_name: Mapped[str] = mapped_column(String(128), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    asset_code: Mapped[str] = mapped_column(String(32), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    pre_close: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    pct_chg: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    source_payload_id: Mapped[int] = mapped_column(ForeignKey('raw.source_payload.id'), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
