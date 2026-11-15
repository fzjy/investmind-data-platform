"""清点底座运行入口和已有数据库记录。不会启动作业、供应商客户端或服务。"""
import argparse
import ast
import json
from datetime import date, datetime
from pathlib import Path
import re
import sys


def registered_jobs(root):
    path = root / "backend/app/jobs/scheduler.py"
    jobs = {}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_job":
            args = {kw.arg: kw.value for kw in node.keywords}
            name = ast.literal_eval(args["id"])
            jobs[name] = {"triggerExpression": ast.unparse(args["trigger"]), "line": node.lineno}
    return dict(sorted(jobs.items()))


def safe_config(root):
    def read(rel):
        return json.loads((root / rel).read_text(encoding="utf-8"))

    def policy(data, keys):
        return {k: data[k] for k in keys if k in data}

    fin = read("config/stocks/financial-indicators.json")
    market = read("config/market/market-data.json")
    extensions = read("config/macro/macro-extensions.json")["groups"]
    portfolio = read("config/portfolio/portfolio.json")["routine"]
    news = read("config/news/news-collector.json")
    allowed = ("enabled", "maxRequestsPerRun", "maxRetries", "timeoutSeconds", "refreshIntervalDays",
               "historyDays", "maxPagesPerRun", "revisitDays", "maxChecks", "retryDays", "backoffMinutes",
               "maxHttpPerRun", "historyYears", "refreshDays")
    compose = (root / "docker-compose.backend.yml").read_text(encoding="utf-8")
    return {
        "schedulerComposeEnvNames": sorted(re.findall(r"^      (DANG_[A-Z0-9_]+):", compose.split("\n  scheduler:\n")[1], re.M)),
        "marketSchedule": market["schedule"],
        "marketLimits": {key: policy(value, allowed) for key, value in market.items()
                         if isinstance(value, dict) and "maxRequestsPerRun" in value},
        "financialPolicies": {k: policy(v, allowed) for k, v in fin.items() if isinstance(v, dict) and "enabled" in v},
        "macroExtensions": {k: {"enabled": v["collectionEnabled"], "schedule": v["schedule"],
                                "limits": policy(v["limits"], ("maxHttpPerRun", "maxHttpPerPeriod", "maxRetries"))}
                            for k, v in extensions.items()},
        "portfolioRoutine": {k: v for k, v in portfolio.items() if type(v) in (int, bool)},
        "news": {k: v for k, v in news.items() if type(v) in (int, bool)},
    }


def db_snapshot(factory):
    from sqlalchemy import text

    queries = {
        "transaction": "select now() as checked_at, current_setting('transaction_read_only') as read_only, current_setting('transaction_isolation') as isolation",
        "runs": """select distinct on (job_name) job_name, count(*) over (partition by job_name) as total,
            status, started_at, finished_at, case when details->>'trigger' in ('scheduler','cli','manual','startup','bootstrap')
            then details->>'trigger' end as trigger from ops.job_run order by job_name, started_at desc, id desc""",
        "runGroups": """select job_name,status, case when details->>'trigger' in ('scheduler','cli','manual','startup','bootstrap')
            then details->>'trigger' else 'unspecified_or_other' end as trigger, count(*) as total, max(started_at) as latest_start
            from ops.job_run group by 1,2,3 order by 1,2,3""",
        "cursors": """select job_name, count(*) as total, max(updated_at) as latest_update from ops.job_cursor group by 1 order by 1""",
        "heartbeat": "select count(*) as total, max(last_heartbeat_at) as latest_heartbeat from ops.scheduler_heartbeat",
        "checkpoint": "select count(*) as total, max(last_evaluated_at) as latest_evaluation from ops.scheduler_checkpoint",
        "macroSeries": "select count(*) as total, max(last_checked_at) as latest_check, max(last_success_at) as latest_success from ops.macro_series_state",
        "failures": """select retryable, resolved_at is not null as resolved, count(*) as total,
            max(coalesce(last_attempted_at,created_at)) as latest_attempt from ops.job_failure group by 1,2 order by 1,2""",
        "newsDelivery": "select delivered, count(*) as total, max(fetched_at) as latest_fetch from ops.news_delivery group by 1 order by 1",
        "ingestion": """select source,dataset,count(*) as batches,max(started_at) as latest_start,
            max(finished_at) as latest_finish from raw.ingestion_batch group by 1,2 order by 1,2""",
    }
    with factory() as session:
        evidence = {}
        for name, sql in queries.items():
            evidence[name] = [dict(row) for row in session.execute(text(sql)).mappings()]
        assert evidence["transaction"][0]["read_only"] == "on"
        assert evidence["transaction"][0]["isolation"] == "repeatable read"
        return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path[:0] = [str(root / "backend"), str(root / "packages/news-collector/src")]
    from typer.main import get_command
    from app.cli.main import app
    from app.config import Settings
    from app.shared.readonly_database import open_readonly_factory

    registry = get_command(app).commands
    commands = {name: cmd.callback.__module__ for name, cmd in sorted(registry.items())}
    cfg = Settings.from_env()
    if not cfg.database_read_url:
        parser.error("请提供底座只读数据库连接 DANG_DATABASE_READ_URL")
    with open_readonly_factory(cfg.database_read_url) as factory:
        db = db_snapshot(factory)
    result = {"schemaVersion": 1, "schedulerJobs": registered_jobs(root), "cliCommands": commands,
              "repositoryConfig": safe_config(root), "database": db}
    def dates(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        raise TypeError(type(value).__name__)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=dates) + "\n", encoding="utf-8")
    print(f"jobs={len(result['schedulerJobs'])} cli={len(commands)} job_logs={len(db['runs'])} cursor_groups={len(db['cursors'])}")


if __name__ == "__main__":
    main()
