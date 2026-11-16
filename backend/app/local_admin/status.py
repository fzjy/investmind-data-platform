from datetime import UTC, datetime, timedelta
from sqlalchemy import select
from app.db.models import JobCursor, JobRun, SchedulerHeartbeat
from app.local_admin.configuration import MARKET_TASKS, TASKS
from app.local_admin.tables import RUN_RELATIONS, reading

RELATED = {
    "stocks": ("core", "daily_price"), "announcements": ("raw", "ingestion_batch"),
    "macro": ("ops", "job_run"), "news": ("ops", "news_delivery"),
}


def task_rows(manager):
    rows = manager.store.rows()
    result = []
    for name, (title, description, job_id) in TASKS.items():
        row = rows[name]
        worker = "news-worker" if name == "news" else "scheduler"
        process = manager.workers.get(worker)
        alive = bool(process and process.poll() is None)
        fresh = row["seen"] and datetime.now(UTC) - datetime.fromisoformat(row["seen"]) < timedelta(seconds=15)
        actual = "执行中" if row["active"] and alive else "空闲"
        if (row["active"] or row["enabled"]) and (not alive or (name != "news" and not fresh)):
            actual = "状态待确认"
        elif not row["enabled"] and not row["active"]:
            actual = "已停用"
        error = (manager.failed.get(worker) if row["enabled"] else None) or row["error"]
        applied = row["applied"] == row["generation"] and row["effective"] == row["rule"]
        if name != "news" and row["enabled"] and not fresh:
            applied = False
        source = ("新闻采集配置" if name == "news" else "市场行情配置" if name in MARKET_TASKS
                  else "DANG_MACRO_SYNC_*" if name == "macro" else "DANG_SYNC_HOUR / DANG_SYNC_MINUTE")
        result.append({**row, "title": title, "description": description, "jobId": job_id, "source": source,
                       "applyState": error or ("已生效" if applied else "已保存，待应用"),
                       "activity": actual, "workerAlive": alive})
    return result


def run_history(engine, name, run_id=None, offset=0):
    job_id = TASKS[name][2]
    stmt = select(JobRun.id, JobRun.status, JobRun.business_key, JobRun.started_at, JobRun.finished_at,
                  JobRun.row_count, JobRun.request_count, JobRun.details["trigger"].as_string().label("trigger")
                  ).where(JobRun.job_name == job_id)
    if run_id:
        stmt = stmt.where(JobRun.id == run_id)
    with reading(engine) as conn:
        runs = conn.execute(stmt.order_by(JobRun.started_at.desc(), JobRun.id.desc()).offset(offset).limit(21)).mappings().all()
        if run_id and not runs:
            raise KeyError(run_id)
        cursor_stmt = select(JobCursor.cursor_key, JobCursor.updated_at)
        if name == "news":
            cursor_stmt = cursor_stmt.where(JobCursor.job_name == "news-collector", JobCursor.cursor_key == "sina-finance")
        elif name == "announcements":
            cursor_stmt = cursor_stmt.where(JobCursor.job_name == "workbench-cninfo-history-v1")
        else:
            cursor_stmt = cursor_stmt.where(JobCursor.job_name == job_id)
        cursor = conn.execute(cursor_stmt.order_by(JobCursor.updated_at.desc()).limit(1)).mappings().first()
        beat = conn.execute(select(SchedulerHeartbeat.state, SchedulerHeartbeat.last_heartbeat_at)
                            .order_by(SchedulerHeartbeat.last_heartbeat_at.desc()).limit(1)).mappings().first()
    records = []
    for run in runs[:20]:
        data = dict(run)
        data["displayStatus"] = "运行记录未结束，状态待确认" if run["status"] == "running" else run["status"]
        if run["finished_at"] and run["started_at"]:
            data["duration"] = (run["finished_at"] - run["started_at"]).total_seconds()
        records.append(data)
    return {"runs": records, "more": len(runs) > 20, "cursor": dict(cursor) if cursor else None,
            "heartbeat": dict(beat) if beat else None, "related": RELATED.get(name, ("raw", "ingestion_batch")),
            "relations": list(RUN_RELATIONS) if run_id else []}
