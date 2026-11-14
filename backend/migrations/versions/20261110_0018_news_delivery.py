"""新闻待交付状态统一进入主库；正文继续使用既有core.content_document。"""
from alembic import op
import sqlalchemy as sa

revision = '20261110_0018'
down_revision = '20261022_0017'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('news_delivery', sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('source', sa.String(64), nullable=False), sa.Column('external_id', sa.String(256), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False), sa.Column('delivered', sa.Boolean(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(timezone=True), nullable=False), schema='ops')
    op.create_index('ix_news_delivery_source_external', 'news_delivery', ['source', 'external_id', 'id'], schema='ops')
    op.create_index('ix_news_delivery_pending', 'news_delivery', ['source', 'delivered', 'id'], schema='ops')


def downgrade():
    raise RuntimeError('NEWS_DELIVERY_DOWNGRADE_REQUIRES_EXPLICIT_DATA_MIGRATION')
