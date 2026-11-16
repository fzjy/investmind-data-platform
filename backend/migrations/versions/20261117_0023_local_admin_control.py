"""本机任务控制与执行历史。"""
from alembic import op
import sqlalchemy as sa
revision = '20261117_0023'
down_revision = '20261116_0022'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('local_admin_task',
        sa.Column('name', sa.Text(), primary_key=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('rule', sa.Text(), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('revision', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('recovered', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('applied', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('effective', sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('started', sa.DateTime(timezone=True)),
        sa.Column('finished', sa.DateTime(timezone=True)),
        sa.Column('next_run', sa.DateTime(timezone=True)),
        sa.Column('seen', sa.DateTime(timezone=True)),
        sa.Column('result', sa.Text()),
        sa.Column('error', sa.Text()),
        sa.Column('owner', sa.String(128)), schema='ops')
    op.create_table('local_admin_execution',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('recovery', sa.Boolean(), nullable=False),
        sa.Column('started', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished', sa.DateTime(timezone=True)),
        sa.Column('result', sa.Text()),
        sa.Column('owner', sa.String(128)), schema='ops')
    op.create_index('ix_local_admin_execution_by_task', 'local_admin_execution', ['name', sa.text('id DESC')], schema='ops')

def downgrade():
    op.drop_table('local_admin_execution', schema='ops')
    op.drop_table('local_admin_task', schema='ops')
