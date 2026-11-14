"""REA多字段独立证据及冻结计划；仅加表，不触碰旧数据。"""
import sqlalchemy as sa
from alembic import op

revision = '20261021_0015'
down_revision = '20261020_0014'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = (('ops','enterprise_rea_plan'),('ops','enterprise_rea_attempt'),
              ('raw','enterprise_rea_response'),('core','enterprise_rea_supplement'))
    if any(inspector.has_table(name,schema=schema) for schema,name in tables):
        raise RuntimeError('ENTERPRISE_REA_TABLE_ALREADY_EXISTS')
    op.create_table('enterprise_rea_plan',
        sa.Column('fingerprint',sa.String(64),primary_key=True),
        sa.Column('sequence_number',sa.Integer(),nullable=False,unique=True),
        sa.Column('snapshot',sa.JSON(),nullable=False),
        sa.Column('snapshot_hash',sa.String(64),nullable=False),
        sa.Column('witness_version',sa.String(64),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),schema='ops')
    op.create_table('enterprise_rea_attempt',
        sa.Column('request_key',sa.String(64),primary_key=True),
        sa.Column('plan_fingerprint',sa.String(64),sa.ForeignKey('ops.enterprise_rea_plan.fingerprint'),nullable=False),
        sa.Column('run_id',sa.String(36),nullable=False),sa.Column('request',sa.JSON(),nullable=False),
        sa.Column('page_size',sa.Integer(),nullable=False),sa.Column('state',sa.String(16),nullable=False),
        sa.Column('error_code',sa.String(64)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('finished_at',sa.DateTime(timezone=True)),
        sa.CheckConstraint("state IN ('reserved','received','failed')",name='rea_attempt_state'),schema='ops')
    op.create_table('enterprise_rea_response',
        sa.Column('request_key',sa.String(64),sa.ForeignKey('ops.enterprise_rea_attempt.request_key'),primary_key=True),
        sa.Column('payload',sa.JSON(),nullable=False),sa.Column('response_hash',sa.String(64),nullable=False),
        sa.Column('captured_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),schema='raw')
    op.create_table('enterprise_rea_supplement',
        sa.Column('request_key',sa.String(64),sa.ForeignKey('raw.enterprise_rea_response.request_key'),primary_key=True),
        sa.Column('core_binding',sa.JSON(),nullable=False),sa.Column('witness_version',sa.String(64),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),sa.Column('outcome',sa.JSON(),nullable=False),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('matched','response-witness-missing','no-match','ambiguous','truncated','identity-mismatch')",name='rea_supplement_status'),schema='core')
    op.execute("""CREATE FUNCTION ops.reject_enterprise_rea_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'ENTERPRISE_REA_IMMUTABLE'; END $$""")
    for schema,name in (tables[0],*tables[2:]):
        op.execute(f'CREATE TRIGGER immutable_enterprise_rea BEFORE UPDATE OR DELETE ON {schema}.{name} '
                   'FOR EACH ROW EXECUTE FUNCTION ops.reject_enterprise_rea_mutation()')


def downgrade():
    raise RuntimeError('ENTERPRISE_REA_DOWNGRADE_DISABLED')
