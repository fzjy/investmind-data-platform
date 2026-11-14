"""成分候选两表及不可变保护；不改旧成分/财报/指针。"""
from alembic import op
import sqlalchemy as sa

revision = '20261022_0016'
down_revision = '20261021_0015'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    tables = (('raw', 'enterprise_target_observation'), ('core', 'enterprise_target_candidate'))
    if any(inspector.has_table(name, schema=schema) for schema, name in tables):
        raise RuntimeError('TARGET_EVIDENCE_TABLE_ALREADY_EXISTS')
    op.create_table('enterprise_target_observation',
        sa.Column('observation_id', sa.String(64), primary_key=True),
        sa.Column('provider', sa.String(16), nullable=False),
        sa.Column('target_code', sa.String(64), nullable=False),
        sa.Column('target_kind', sa.String(16), nullable=False),
        sa.Column('api_name', sa.String(64), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('requested_fields', sa.JSON(), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('response', sa.JSON(), nullable=False),
        sa.Column('response_hash', sa.String(64), nullable=False),
        sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider = 'tushare'", name='target_provider'), schema='raw')
    op.create_table('enterprise_target_candidate',
        sa.Column('candidate_id', sa.String(64), primary_key=True),
        sa.Column('observation_id', sa.String(64), sa.ForeignKey('raw.enterprise_target_observation.observation_id'), nullable=False),
        sa.Column('normalizer_version', sa.String(64), nullable=False),
        sa.Column('policy_hash', sa.String(64), nullable=False),
        sa.Column('as_of', sa.Date(), nullable=False),
        sa.Column('timezone', sa.String(64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('observation_id', 'normalizer_version', 'policy_hash', 'as_of', 'timezone',
            name='uq_enterprise_target_candidate_context'), schema='core')
    op.execute("""CREATE FUNCTION ops.reject_enterprise_target_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'TARGET_EVIDENCE_IMMUTABLE'; END $$""")
    for schema, name in tables:
        op.execute(f'CREATE TRIGGER immutable_enterprise_target BEFORE UPDATE OR DELETE ON {schema}.{name} '
            'FOR EACH ROW EXECUTE FUNCTION ops.reject_enterprise_target_mutation()')


def downgrade():
    raise RuntimeError('TARGET_EVIDENCE_DOWNGRADE_DISABLED')
