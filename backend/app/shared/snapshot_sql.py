"""原生 SQL 消费者的当前产品关系；同一条语句可兼容迁移前后的数据库。"""
PUBLISHED_SNAPSHOTS_SQL = """(
    SELECT p.product,p.version,p.as_of,
           CASE WHEN json_typeof(p.payload) <> 'string' THEN p.payload ELSE (SELECT h.payload FROM mart.page_snapshot h
               WHERE h.product=p.product AND h.snapshot_version=p.version) END AS payload,
           p.stale,p.stale_reason,p.freshness_checked_at,p.published_at
    FROM mart.published_snapshot p
)"""
