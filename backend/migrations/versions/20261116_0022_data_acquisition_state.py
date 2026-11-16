"""按对象及条目记录数据获取凭证。"""
from alembic import op
import sqlalchemy as sa

revision = '20261116_0022'
down_revision = '20261115_0021'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'data_acquisition_state',
        sa.Column('entry_id', sa.String(128), primary_key=True),
        sa.Column('item_id', sa.String(128), primary_key=True),
        sa.Column('subject_kind', sa.String(24), primary_key=True),
        sa.Column('subject_key', sa.String(160), primary_key=True),
        sa.Column('scope_kind', sa.String(16), primary_key=True),
        sa.Column('attempt_id', sa.String(128), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('scope', sa.JSON(), nullable=False),
        sa.Column('trigger', sa.String(32), nullable=False),
        sa.Column('terminal', sa.JSON(), nullable=False),
        sa.Column('last_success', sa.JSON(), nullable=False),
        sa.Column('failures', sa.JSON(), nullable=False),
        sa.CheckConstraint("scope_kind in ('routine','historical')", name='scope_kind'),
        schema='ops',
    )
    op.create_index('ix_acquisition_subject','data_acquisition_state',
                    ['subject_kind','subject_key','entry_id'], schema='ops')


def downgrade():
    op.drop_table('data_acquisition_state', schema='ops')
