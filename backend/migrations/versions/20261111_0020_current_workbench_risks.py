"""工作台风险结果关联当前发布版本，迁移期间允许旧行与新结构共存。"""
from alembic import op
import sqlalchemy as sa

revision = "20261111_0020"
down_revision = "20261111_0019"
branch_labels = None
depends_on = None


def upgrade():
    schema = "derived" if op.get_bind().dialect.name == "postgresql" else None
    inspector = sa.inspect(op.get_bind())
    columns = {col["name"] for col in inspector.get_columns("risk_result", schema=schema)}
    for name in ("snapshot_version", "rule_version"):
        if name not in columns:
            op.add_column("risk_result", sa.Column(name, sa.String(64), nullable=True), schema=schema)
    if "uq_current_risk_security_rule" in {i["name"] for i in inspector.get_indexes("risk_result", schema=schema)}:
        return
    op.create_index("uq_current_risk_security_rule", "risk_result", ["ts_code", "risk_id"],
                    unique=True, schema=schema, postgresql_where=sa.text("snapshot_version IS NOT NULL"),
                    sqlite_where=sa.text("snapshot_version IS NOT NULL"))


def downgrade():
    schema = "derived" if op.get_bind().dialect.name == "postgresql" else None
    op.drop_index("uq_current_risk_security_rule", table_name="risk_result", schema=schema)
    for name in ("rule_version", "snapshot_version"):
        op.drop_column("risk_result", name, schema=schema)
