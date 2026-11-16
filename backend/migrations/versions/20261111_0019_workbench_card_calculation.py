"""保存当前股票指标的版本化计算明细，旧行可空。"""
from alembic import op
import sqlalchemy as sa

revision = "20261111_0019"
down_revision = "20261110_0018"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    schema = "derived"
    columns = sa.inspect(bind).get_columns("metric_value", schema=schema)
    if "calculation" not in {column["name"] for column in columns}:
        op.add_column("metric_value", sa.Column("calculation", sa.JSON(), nullable=True), schema=schema)


def downgrade():
    schema = "derived"
    op.drop_column("metric_value", "calculation", schema=schema)
