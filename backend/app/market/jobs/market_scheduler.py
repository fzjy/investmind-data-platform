from datetime import UTC, datetime, time, timedelta

from apscheduler.triggers.cron import CronTrigger

from app.market.data.catalog import MarketDataCatalog, MarketDailySchedule
from app.market.data.runtime import run_market_step
from app.market.pulse.runtime import run_pulse_step
from app.market.region.runtime import run_region_step
from app.market.etf.runtime import run_etf_step
from app.market.commodities.runtime import run_commodity_step
from app.market.data.scheduling import most_recent_market_schedule, next_market_schedule


class MarketDailyCronTrigger(CronTrigger):
    def __init__(self, schedule):
        super().__init__(hour=schedule.time.hour, minute=schedule.time.minute, timezone=schedule.timezone)

    def get_next_fire_time(self, previous_fire_time, now):
        fields = {field.name:str(field) for field in self.fields}
        schedule = MarketDailySchedule(time=time(int(fields['hour']), int(fields['minute'])), timezone=str(self.timezone))
        earliest = previous_fire_time.astimezone(UTC) + timedelta(microseconds=1) if previous_fire_time is not None else now.astimezone(UTC)
        latest = most_recent_market_schedule(earliest, schedule)
        return latest if latest == earliest else next_market_schedule(earliest, schedule)


class MarketScheduledCoordinator:
    def __init__(self, settings, *, runner=run_market_step):
        self.settings, self.runner = settings, runner
        self.catalog = MarketDataCatalog.load() if settings.routine_market_enabled else None

    def run_scheduled(self, now: datetime):
        if self.catalog is None or not self.catalog.enabled:
            return None
        return self.runner(self.settings, now=now, dry_run=False, publish=True)

    def run_startup(self, now: datetime):
        if self.catalog is None or not self.catalog.schedule.catchup_on_start:
            return None
        return self.run_scheduled(now)


class PulseScheduledCoordinator(MarketScheduledCoordinator):
    def __init__(self, settings, *, runner=run_pulse_step):
        self.settings, self.runner = settings, runner
        self.catalog = MarketDataCatalog.load() if settings.routine_market_pulse_enabled else None

    def run_scheduled(self, now: datetime):
        if self.catalog is None or not self.catalog.pulse.enabled:
            return None
        return super().run_scheduled(now)


class RegionScheduledCoordinator(MarketScheduledCoordinator):
    def __init__(self, settings, *, runner=run_region_step):
        self.settings, self.runner = settings, runner
        self.catalog = MarketDataCatalog.load() if settings.routine_region_enabled else None

    def run_scheduled(self, now: datetime):
        if self.catalog is None or not self.catalog.region.enabled:
            return None
        return super().run_scheduled(now)


class EtfScheduledCoordinator(MarketScheduledCoordinator):
    def __init__(self, settings, *, runner=run_etf_step):
        self.settings, self.runner = settings, runner
        self.catalog = MarketDataCatalog.load() if settings.routine_etf_heatmap_enabled else None

    def run_scheduled(self, now: datetime):
        if self.catalog is None or not self.catalog.etf_heatmap.enabled:
            return None
        return super().run_scheduled(now)


class CommodityScheduledCoordinator(MarketScheduledCoordinator):
    def __init__(self, settings, *, runner=run_commodity_step):
        self.settings,self.runner=settings,runner
        self.catalog=MarketDataCatalog.load() if settings.routine_commodities_enabled else None

    def run_scheduled(self, now:datetime):
        if self.catalog is None or not self.catalog.commodities.enabled:
            return None
        return super().run_scheduled(now)
