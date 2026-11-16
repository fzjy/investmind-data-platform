"""本机调度器控制。所有业务调用仍通过原运行时的操作锁、限速与异常处理。"""
from functools import partial

from apscheduler.triggers.cron import CronTrigger
from app.local_admin.configuration import TASKS
from app.local_admin.control import OwnerLost
from app.macro.jobs.macro_scheduler import MacroWeeklyCronTrigger
from app.market.jobs.market_scheduler import (
    MarketDailyCronTrigger, MarketScheduledCoordinator, PulseScheduledCoordinator,
    RegionScheduledCoordinator, EtfScheduledCoordinator, CommodityScheduledCoordinator,
)

MARKET = {
    "market": ("market", "routine_market_enabled", MarketScheduledCoordinator, None),
    "marketPulse": ("pulse", "routine_market_pulse_enabled", PulseScheduledCoordinator, "pulse"),
    "regions": ("region", "routine_region_enabled", RegionScheduledCoordinator, "region"),
    "etfHeatmap": ("etf", "routine_etf_heatmap_enabled", EtfScheduledCoordinator, "etf_heatmap"),
    "commodities": ("commodity", "routine_commodities_enabled", CommodityScheduledCoordinator, "commodities"),
}


def summarize(name, result, recovery):
    if result is None:
        return "未执行或无可确认的记录"
    if hasattr(result, "remaining_dates"):
        remaining = list(result.remaining_dates)
        attempted = len(result.attempted_dates)
        if remaining:
            return f"仍有积压：{len(remaining)} 个交易日（{min(remaining)} 至 {max(remaining)}），本轮尝试 {attempted} 日"
        if any(getattr(r, "status", "") in {"failed", "partial", "locked"}
               for r in getattr(result, "reports", ())):
            return f"部分交易日需核对，本轮尝试 {attempted} 日"
        return f"检查点范围内已补齐，本轮尝试 {attempted} 个交易日"
    status = result.get("status") if isinstance(result, dict) else getattr(result, "status", None)
    if isinstance(result, dict) and type(result.get("pendingCount")) is int and result["pendingCount"] > 0:
        return f"{result.get('asOf') or '本轮'} 仍有积压：{result['pendingCount']} 项（{status}），按检查点续采"
    if status in {"failed", "partial", "blocked", "locked", "delivery_failed"}:
        return f"本轮结果：{status}，请核对处理记录"
    if recovery and name != "stocks":
        if name == "regions":
            return f"更新最新地域映射（{status or '待确认'}），历史映射无法逐日补齐"
        return f"按来源可用窗口检查（{status or '待确认'}），完整历史覆盖待核对"
    return str(status or "处理完毕，覆盖范围待确认")


class ManagedSchedule:
    def __init__(self, store, runtime, make_macro, *, stop_requested=lambda: False):
        self.store = store
        self.runtime = runtime
        self.scheduler = runtime._scheduler
        self.make_macro = make_macro
        self.stop_requested = stop_requested
        self.registered = {}

    def install(self):
        # 普通启动入口仍用原注册路径；本机逐组注册及恢复，避免额外跑全组启动作业。
        for job in self.scheduler.get_jobs():
            if job.id not in {"scheduler-heartbeat", "snapshot-freshness"}:
                self.scheduler.remove_job(job.id)
        # 与心跳相同，控制回调独立于采集锁，但参与退出时的在途回调计数。
        control_tick = self.runtime._guard_job(self.tick, "heartbeat")
        self.scheduler.add_job(control_tick, "interval", seconds=1, id="local-admin-control",
                               next_run_time=self.runtime._now(), max_instances=1, coalesce=True,
                               executor="control")

    def _configure(self, row):
        name = row["name"]
        rt = self.runtime
        settings = rt._settings
        if name == "macro":
            rt._macro_coordinator = self.make_macro(settings.model_copy(update={"macro_sync_enabled": True}))
            return (MacroWeeklyCronTrigger(settings.macro_sync_day_of_week, settings.macro_sync_time,
                                           settings.sync_timezone), "macro", rt._run_macro, partial(rt._run_macro, startup=True))
        if name in MARKET:
            alias, flag, constructor, group = MARKET[name]
            active = settings.model_copy(update={flag: True})
            coordinator = constructor(active)
            catalog = coordinator.catalog
            if row["rule"] != f"每天 {catalog.schedule.time:%H:%M} · {catalog.schedule.timezone}":
                raise ValueError("市场调度配置与应用版本不符")
            if not catalog.enabled or (group and not getattr(catalog, group).enabled):
                raise ValueError("来源配置已关闭")
            setattr(rt, "_" + alias + "_coordinator", coordinator)
            work = getattr(rt, "_run_" + alias)
            reason = "commodities" if name == "commodities" else alias
            return MarketDailyCronTrigger(catalog.schedule), reason, work, partial(work, startup=True)
        trigger = CronTrigger(hour=settings.sync_hour, minute=settings.sync_minute, timezone=settings.sync_timezone)
        if name == "announcements":
            return trigger, name, rt._run_announcements, rt._run_announcements
        def restore_stocks():
            rt._coordinator.recover_interrupted(rt._now())
            if rt._prepare_checkpoint(rt._now()) is False:
                return {"status": "日历不足，范围待确认"}
            report = rt._run_startup()
            if rt._calendar_alert_is_open():
                return {"status": "日历不足，范围待确认"}
            return report
        return trigger, "daily", rt._run_daily, restore_stocks

    def _call(self, row, work, reason, recovery):
        name = row["name"]
        def admitted():
            execution_id = self.store.admit(name, row["generation"], recovery=recovery,
                                    revision=None if recovery else row["revision"])
            if execution_id is None:
                return
            try:
                result = work()
                summary = summarize(name, result, recovery)
            except BaseException:
                self.store.finish(name, execution_id, "失败，请查看运行记录")
                raise
            self.store.finish(name, execution_id, summary)
        return self.runtime._guard_job(admitted, reason)

    def tick(self):
        try:
            rows = None if self.stop_requested() else self.store.rows()
        except OwnerLost:
            rows = None
        if rows is None:
            # 控制线程不能 join 自己。唤醒 BlockingScheduler，由 run() 的 finally 等在途作业。
            with self.runtime._jobs_condition:
                self.runtime._jobs_stopping = True
            self.scheduler.shutdown(wait=False)
            return
        rt = self.runtime
        flags = {flag: bool(rows[name]["enabled"]) for name, (_, flag, _, _) in MARKET.items()}
        flags["scheduler_announcements_enabled"] = bool(rows["announcements"]["enabled"])
        rt._settings = rt._settings.model_copy(update=flags)
        for name, row in rows.items():
            if name == "news":
                continue
            version = (row["generation"], row["revision"])
            job_id = TASKS[name][2]
            if self.registered.get(name) != version:
                for obsolete in (job_id, "restore-" + name):
                    if self.scheduler.get_job(obsolete):
                        self.scheduler.remove_job(obsolete)
                try:
                    if row["enabled"]:
                        trigger, reason, work, recover = self._configure(row)
                        regular = self._call(row, work, reason, False)
                        self.scheduler.add_job(regular, trigger, id=job_id, max_instances=1, coalesce=True)
                        if row["recovered"] != row["generation"]:
                            recovery = self._call(row, recover, reason, True)
                            self.scheduler.add_job(recovery, "date", run_date=rt._now(), id="restore-" + name,
                                                   misfire_grace_time=None)
                    self.registered[name] = version
                except Exception as exc:
                    self.store.acknowledge(row, error=f"注册失败（{type(exc).__name__}）")
                    continue
            job = self.scheduler.get_job(job_id)
            next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
            self.store.acknowledge(row, next_run)
