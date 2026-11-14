"""单根执行预留，独立于既有成分证据/财报/发布。"""
from alembic import op
import sqlalchemy as sa

revision='20261022_0017'
down_revision='20261022_0016'
branch_labels=None
depends_on=None


def upgrade():
    inspector=sa.inspect(op.get_bind())
    if any(inspector.has_table(name,schema='ops') for name in ('enterprise_target_execution_plan','enterprise_target_execution_attempt')):
        raise RuntimeError('TARGET_EXECUTION_TABLE_ALREADY_EXISTS')
    op.create_table('enterprise_target_execution_plan',
        sa.Column('slot',sa.String(32),primary_key=True),
        sa.Column('fingerprint',sa.String(64),nullable=False,unique=True),
        sa.Column('snapshot',sa.JSON(),nullable=False),sa.Column('snapshot_hash',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("slot = 'bootstrap'",name='target_execution_slot'),schema='ops')
    op.create_table('enterprise_target_execution_attempt',
        sa.Column('request_key',sa.String(64),primary_key=True),
        sa.Column('plan_fingerprint',sa.String(64),sa.ForeignKey('ops.enterprise_target_execution_plan.fingerprint'),nullable=False),
        sa.Column('run_id',sa.String(36),nullable=False),sa.Column('request',sa.JSON(),nullable=False),
        sa.Column('request_hash',sa.String(64),nullable=False),sa.Column('state',sa.String(16),nullable=False),
        sa.Column('observation_id',sa.String(64),sa.ForeignKey('raw.enterprise_target_observation.observation_id')),
        sa.Column('error_code',sa.String(64)),sa.Column('created_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('finished_at',sa.DateTime(timezone=True)),
        sa.CheckConstraint("state = 'reserved' OR state = 'received' OR state = 'failed'",name='target_execution_state'),schema='ops')
    op.execute("""CREATE FUNCTION ops.reject_enterprise_target_execution_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'TARGET_EXECUTION_IMMUTABLE'; END $$""")
    op.execute('CREATE TRIGGER immutable_enterprise_target_execution BEFORE UPDATE OR DELETE '
        'ON ops.enterprise_target_execution_plan FOR EACH ROW EXECUTE FUNCTION ops.reject_enterprise_target_execution_mutation()')


def downgrade():
    raise RuntimeError('TARGET_EXECUTION_DOWNGRADE_DISABLED')
