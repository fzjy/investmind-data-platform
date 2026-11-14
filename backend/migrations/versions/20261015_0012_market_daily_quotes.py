"""独立市场日线报价，不修改股票/宏观或任何发布指针。"""
import sqlalchemy as sa
from alembic import op

revision = '20261015_0012'
down_revision = '20261014_0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic重复upgrade不会重复执行本迁移；意外同名表不能静默接管。
    if sa.inspect(op.get_bind()).has_table('market_daily_quote', schema='core'):
        raise RuntimeError('MARKET_QUOTE_TABLE_ALREADY_EXISTS')
    op.create_table('market_daily_quote',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('provider', sa.String(64), nullable=False),
        sa.Column('api_name', sa.String(128), nullable=False),
        sa.Column('asset_type', sa.String(16), nullable=False),
        sa.Column('asset_code', sa.String(32), nullable=False),
        sa.Column('trade_date', sa.Date(), nullable=False),
        *(sa.Column(key, sa.Numeric(20, 8), nullable=False) for key in ('open','high','low','close','pre_close','pct_chg')),
        sa.Column('source_payload_id', sa.Integer(), sa.ForeignKey('raw.source_payload.id'), nullable=False),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('provider','asset_type','asset_code','trade_date', name='uq_market_daily_quote_identity'),
        sa.CheckConstraint("asset_type IN ('indices','etfs')", name='ck_market_quote_asset_type'),
        sa.CheckConstraint('open > 0 AND high > 0 AND low > 0 AND close > 0 AND pre_close > 0 '
            'AND open < 1000000000000 AND high < 1000000000000 AND low < 1000000000000 '
            'AND close < 1000000000000 AND pre_close < 1000000000000 '
            'AND pct_chg > -1000000000000 AND pct_chg < 1000000000000', name='ck_market_quote_finite'),
        sa.CheckConstraint('low <= open AND open <= high AND low <= close AND close <= high', name='ck_market_quote_ohlc'),
        schema='core')


def downgrade() -> None:
    raise RuntimeError('MARKET_QUOTE_DESTRUCTIVE_DOWNGRADE_DISABLED')
