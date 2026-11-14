"""五种不可变历史产品的当前指针复用其永久历史正文。"""
from alembic import op

revision = "20261115_0021"
down_revision = "20261111_0020"
branch_labels = None
depends_on = None

PRODUCTS = "'macro-insights','macro-transmission','market-overview','portfolio','screener'"


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    # 两张表的读写方在事务提交前不能观察到中间形态。
    op.execute("LOCK TABLE mart.published_snapshot, mart.page_snapshot IN SHARE ROW EXCLUSIVE MODE")
    if op.get_bind().exec_driver_sql("SELECT EXISTS (SELECT 1 FROM mart.published_snapshot WHERE json_typeof(payload) <> 'object')").scalar():
        raise ValueError("已有产品正文不是对象，未修改现存数据")
    op.execute("""ALTER TABLE mart.page_snapshot ADD COLUMN history_generation text
               GENERATED ALWAYS AS (md5(payload::text)) STORED""")
    op.execute("""ALTER TABLE mart.page_snapshot ADD CONSTRAINT uq_page_history_generation
               UNIQUE(product,snapshot_version,history_generation)""")
    op.execute("""ALTER TABLE mart.published_snapshot ADD COLUMN history_generation text
               GENERATED ALWAYS AS (substring(payload::text from '^"@investmind-history:([0-9a-f]{32})"$')) STORED""")
    op.execute("""ALTER TABLE mart.published_snapshot ADD COLUMN history_version varchar(64)
               GENERATED ALWAYS AS (CASE WHEN json_typeof(payload)='string' THEN version ELSE NULL END) STORED""")
    op.execute("""ALTER TABLE mart.published_snapshot ADD CONSTRAINT fk_pointer_history
               FOREIGN KEY(product,history_version,history_generation) REFERENCES mart.page_snapshot(product,snapshot_version,history_generation)""")
    # CASE/OR 形式避免 pg_dump 还原将多个常量的 IN 检查改写为不同的数组表达式。
    allowed = " OR ".join("product="+name for name in PRODUCTS.split(","))
    op.execute(f"ALTER TABLE mart.published_snapshot ADD CONSTRAINT ck_history_products CHECK (json_typeof(payload)='object' OR (history_generation IS NOT NULL AND ({allowed})))")
    op.execute(f"""UPDATE mart.published_snapshot p SET payload=to_json('@investmind-history:' || h.history_generation) FROM mart.page_snapshot h
        WHERE p.product IN ({PRODUCTS}) AND h.product=p.product AND h.snapshot_version=p.version
        AND h.as_of=p.as_of AND p.payload::text=h.payload::text
        AND NOT jsonb_exists_any(h.payload::jsonb, ARRAY['marketNews','dailyChart'])""")


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("LOCK TABLE mart.published_snapshot, mart.page_snapshot IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""UPDATE mart.published_snapshot p SET payload=h.payload FROM mart.page_snapshot h
        WHERE p.history_generation IS NOT NULL AND h.product=p.product AND h.snapshot_version=p.version""")
    op.execute("""ALTER TABLE mart.published_snapshot DROP CONSTRAINT ck_history_products, DROP CONSTRAINT fk_pointer_history, DROP COLUMN history_version, DROP COLUMN history_generation""")
    op.execute("ALTER TABLE mart.page_snapshot DROP CONSTRAINT uq_page_history_generation, DROP COLUMN history_generation")
