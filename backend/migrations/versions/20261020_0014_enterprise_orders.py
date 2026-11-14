"""合同负债独立证据：仅新增三表，不改旧应收/财报/游标。"""
import sqlalchemy as sa
from alembic import op

revision='20261020_0014'
down_revision='20261018_0013'
branch_labels=None
depends_on=None


def upgrade():
    inspector=sa.inspect(op.get_bind())
    tables=(('ops','enterprise_order_attempt'),('raw','enterprise_order_response'),('core','enterprise_order_supplement'))
    if any(inspector.has_table(name,schema=schema) for schema,name in tables):
        raise RuntimeError('ENTERPRISE_ORDER_TABLE_ALREADY_EXISTS')
    op.create_table('enterprise_order_attempt',
        sa.Column('id',sa.String(36),primary_key=True),
        sa.Column('run_id',sa.String(36),nullable=False),
        sa.Column('request_key',sa.String(64),nullable=False),
        sa.Column('plan_fingerprint',sa.String(64),nullable=False),
        sa.Column('request',sa.JSON(),nullable=False),
        sa.Column('page_size',sa.Integer(),nullable=False),
        sa.Column('state',sa.String(16),nullable=False),
        sa.Column('error_code',sa.String(64)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column('finished_at',sa.DateTime(timezone=True)),
        sa.CheckConstraint("state IN ('reserved','received','failed')",name='order_attempt_state'),schema='ops')
    for name in ('run_id','request_key'):
        op.create_index(f'ix_ops_enterprise_order_attempt_{name}','enterprise_order_attempt',[name],schema='ops')
    op.create_table('enterprise_order_response',
        sa.Column('attempt_id',sa.String(36),sa.ForeignKey('ops.enterprise_order_attempt.id'),primary_key=True),
        sa.Column('request_key',sa.String(64),nullable=False),
        sa.Column('payload',sa.JSON(),nullable=False),
        sa.Column('response_hash',sa.String(64),nullable=False),
        sa.Column('captured_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),schema='raw')
    op.create_index('ix_raw_enterprise_order_response_request_key','enterprise_order_response',['request_key'],schema='raw')
    op.create_table('enterprise_order_supplement',
        sa.Column('request_key',sa.String(64),primary_key=True),
        sa.Column('response_id',sa.String(36),sa.ForeignKey('raw.enterprise_order_response.attempt_id'),nullable=False),
        sa.Column('core_binding',sa.JSON(),nullable=False),
        sa.Column('witness_version',sa.String(64),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),
        sa.Column('value',sa.String(512)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('matched','returned-null','source-witness-missing',"
            "'response-witness-missing','no-match','ambiguous','missing-field','invalid-value','truncated','identity-mismatch')",
            name='order_supplement_status'),
        sa.CheckConstraint("(status = 'matched' AND value IS NOT NULL) OR (status <> 'matched' AND value IS NULL)",
            name='order_supplement_value'),schema='core')
    op.execute("""CREATE FUNCTION ops.reject_enterprise_order_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'ENTERPRISE_ORDER_IMMUTABLE'; END $$""")
    for schema,name in tables[1:]:
        op.execute(f'CREATE TRIGGER immutable_enterprise_order BEFORE UPDATE OR DELETE ON {schema}.{name} '
            'FOR EACH ROW EXECUTE FUNCTION ops.reject_enterprise_order_mutation()')


def downgrade():
    raise RuntimeError('ENTERPRISE_ORDER_DOWNGRADE_DISABLED')
