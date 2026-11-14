"""独立企业应收补充证据；只新增表，不改旧财报与游标。"""
import sqlalchemy as sa
from alembic import op

revision = '20261018_0013'
down_revision = '20261015_0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = (('ops','enterprise_receivable_attempt'), ('raw','enterprise_receivable_response'),
        ('core','enterprise_receivable_supplement'))
    if any(inspector.has_table(name, schema=schema) for schema,name in tables):
        raise RuntimeError('ENTERPRISE_RECEIVABLE_TABLE_ALREADY_EXISTS')
    op.create_table('enterprise_receivable_attempt',
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
        sa.CheckConstraint("state IN ('reserved','received','failed')",name='receivable_attempt_state'),schema='ops')
    for name in ('run_id','request_key'):
        op.create_index(f'ix_ops_enterprise_receivable_attempt_{name}','enterprise_receivable_attempt',[name],schema='ops')
    op.create_table('enterprise_receivable_response',
        sa.Column('attempt_id',sa.String(36),sa.ForeignKey('ops.enterprise_receivable_attempt.id'),primary_key=True),
        sa.Column('request_key',sa.String(64),nullable=False),
        sa.Column('payload',sa.JSON(),nullable=False),
        sa.Column('response_hash',sa.String(64),nullable=False),
        sa.Column('captured_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),schema='raw')
    op.create_index('ix_raw_enterprise_receivable_response_request_key','enterprise_receivable_response',['request_key'],schema='raw')
    op.create_table('enterprise_receivable_supplement',
        sa.Column('request_key',sa.String(64),primary_key=True),
        sa.Column('response_id',sa.String(36),sa.ForeignKey('raw.enterprise_receivable_response.attempt_id'),nullable=False),
        sa.Column('core_binding',sa.JSON(),nullable=False),
        sa.Column('witness_version',sa.String(64),nullable=False),
        sa.Column('status',sa.String(32),nullable=False),
        sa.Column('value',sa.String(512)),
        sa.Column('created_at',sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('matched','returned-null','source-witness-missing',"
            "'response-witness-missing','no-match','ambiguous','missing-field','invalid-value','truncated','identity-mismatch')",
            name='receivable_supplement_status'),
        sa.CheckConstraint("(status = 'matched' AND value IS NOT NULL) OR (status <> 'matched' AND value IS NULL)",
            name='receivable_supplement_value'),schema='core')
    # 不使用CREATE OR REPLACE，避免接管意外同名用户函数。
    op.execute("""CREATE FUNCTION ops.reject_enterprise_receivable_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'ENTERPRISE_RECEIVABLE_IMMUTABLE'; END $$""")
    for schema,name in tables[1:]:
        op.execute(f'CREATE TRIGGER immutable_enterprise_receivable BEFORE UPDATE OR DELETE ON {schema}.{name} '
            'FOR EACH ROW EXECUTE FUNCTION ops.reject_enterprise_receivable_mutation()')


def downgrade() -> None:
    raise RuntimeError('ENTERPRISE_RECEIVABLE_DOWNGRADE_DISABLED')
