from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.connectors.tushare.strategies import Dataset, FIELD_REGISTRY, STANDARD_API_NAMES, RequestPlan
from app.db.models import (Security, TradeCalendar, DailyPrice, DailyBasic, AdjustmentFactor,
    DisclosureSchedule, IncomeStatement, BalanceSheet, CashflowStatement, FinancialIndicator)
from app.shared.calendar import calendar_exchange
from app.stocks.dataset_query import calendar_query, financial_rows_query
from app.stocks.universes import UniverseResolver

MARKET = {Dataset.DAILY: DailyPrice, Dataset.DAILY_BASIC: DailyBasic, Dataset.ADJ_FACTOR: AdjustmentFactor}
FINANCIAL = {Dataset.INCOME: IncomeStatement, Dataset.BALANCE_SHEET: BalanceSheet,
    Dataset.CASHFLOW: CashflowStatement, Dataset.FINANCIAL_INDICATOR: FinancialIndicator}
CARD_FIELDS = {Dataset.INCOME: {'basic_eps'}, Dataset.FINANCIAL_INDICATOR: {'eps', 'bps', 'q_eps'}}


def financial_requirement_met(inventory, as_of, since, dataset, fields):
    from app.stocks.derived.inputs import metric_report_revisions
    visible = [r for r in inventory if (r.f_ann_date or r.ann_date) and (r.f_ann_date or r.ann_date) <= as_of]
    if dataset == Dataset.CASHFLOW and 'net_cash_rece_sec' in fields:
        # 桑基图读取合并累计表。同一公告的母公司或单季表不能满足当前合同。
        visible = [row for row in visible if row.report_type == '1']
    required = CARD_FIELDS.get(dataset, set()).intersection(fields)
    # q_eps 已随较早的研报字段合同发布；该冻结版本沿用原来的完成条件。
    if required - {'q_eps'}:
        # 采用与计算一致的版本；母公司和调整前数据不能代替合并累计或单季。
        groups = metric_report_revisions(visible, indicator=dataset == Dataset.FINANCIAL_INDICATOR, as_of=as_of)
        visible = [r for group in groups.values() for r in group]
        if any(not required <= (r.data or {}).keys() for r in visible):
            return False
    return any((r.f_ann_date or r.ann_date) >= since for r in visible)


@dataclass(frozen=True)
class StockUpdateOptions:
    as_of: date | None = None
    max_requests: int = 512
    calendar_lookahead_days: int = 31
    market_years: int = 3
    timezone: str = 'Asia/Shanghai'
    hour: int = 19
    minute: int = 0

    def __post_init__(self):
        if isinstance(self.max_requests, bool) or not 1 <= self.max_requests <= 10000:
            raise ValueError('invalid request budget')
        if not 1 <= self.market_years <= 10 or not 1 <= self.calendar_lookahead_days <= 366:
            raise ValueError('invalid stock window')
        time(self.hour, self.minute)
        ZoneInfo(self.timezone)

    def upper(self, now: datetime) -> date:
        if now.tzinfo is None:
            raise ValueError('aware clock required')
        local = now.astimezone(ZoneInfo(self.timezone))
        upper = local.date() if local.time() >= time(self.hour, self.minute) else local.date()-timedelta(days=1)
        if self.as_of and self.as_of > upper:
            raise ValueError('target date is not completed')
        return self.as_of or upper

    def start(self, as_of: date) -> date:
        return as_of.replace(year=as_of.year-self.market_years, day=min(as_of.day, 28) if as_of.month == 2 else as_of.day)


def plan_for(dataset: Dataset, params: dict[str, str]) -> RequestPlan:
    fields = FIELD_REGISTRY[dataset]
    if dataset == Dataset.DAILY_BASIC:
        fields = tuple(f for f in fields if f != 'limit_status')
    if dataset == Dataset.FINANCIAL_INDICATOR:
        fields = tuple(f for f in fields if f not in {'f_ann_date', 'report_type', 'comp_type', 'update_flag'})
    if dataset == Dataset.DIVIDEND:
        from app.stocks.dividend_facts import SYNC_FIELDS
        fields = SYNC_FIELDS
    cap = {Dataset.DIVIDEND: 2000, Dataset.DISCLOSURE_DATE: 6000, Dataset.FINANCIAL_INDICATOR: 100}.get(dataset, 5000)
    key = dataset.value+':'+':'.join(f'{k}={v}' for k, v in sorted(params.items()))
    return RequestPlan(dataset, STANDARD_API_NAMES[dataset], params, fields, key, cap)


class StockUpdatePlanner:
    def __init__(self, factory, selection, options: StockUpdateOptions):
        if selection != UniverseResolver().resolve('representative-50'):
            raise ValueError('only fixed representative-50 is supported')
        self.factory, self.selection, self.options = factory, selection, options

    def calendar_plans(self, upper: date):
        return tuple(plan_for(Dataset.TRADE_CALENDAR, {'exchange': exchange,
            'start_date': (self.options.start(upper)-timedelta(days=31)).strftime('%Y%m%d'),
            'end_date': (upper+timedelta(days=self.options.calendar_lookahead_days)).strftime('%Y%m%d')})
            for exchange in ('SSE', 'SZSE'))

    def target(self, upper: date) -> date:
        start = self.options.start(upper)
        with self.factory() as s:
            records = s.scalars(calendar_query(start-timedelta(days=31), upper)
                                .where(TradeCalendar.exchange.in_(('SSE', 'SZSE')))).all()
            calendar = {(r.exchange, r.cal_date): r.is_open for r in records}
        day, opened = start, []
        while day <= upper:
            states = [calendar.get((ex, day)) for ex in ('SSE', 'SZSE')]
            if any(type(v) is not bool for v in states) or states[0] != states[1]:
                raise ValueError('calendar incomplete or conflicting')
            if states[0]:
                opened.append(day)
            day += timedelta(days=1)
        if not opened:
            raise ValueError('calendar has no open day')
        target = opened[-1]
        day = self.options.start(target)
        while day <= target:
            states = [calendar.get((ex, day)) for ex in ('SSE', 'SZSE')]
            if any(type(v) is not bool for v in states) or states[0] != states[1]:
                raise ValueError('target history calendar incomplete or conflicting')
            day += timedelta(days=1)
        return target

    def preflight(self, now: datetime) -> dict:
        upper = self.options.upper(now)
        try:
            target = self.target(upper)
        except ValueError:
            target = None
        return {'upperBound': upper.isoformat(), 'asOf': target.isoformat() if target else None,
            'calendarPending': target is None, 'pool': self.selection.to_payload(),
            'maxRequestsPerRun': self.options.max_requests, 'publication': 'unchanged'}

    def stage_plans(self, stage: str, as_of: date, *, backfill_card_fields=True, legacy_cashflow=False):
        start = self.options.start(as_of)
        if stage == 'actions':
            return tuple(plan_for(d, {'ts_code': c}) for c in self.selection.symbols
                for d in (Dataset.DIVIDEND, Dataset.DISCLOSURE_DATE))
        plans = []
        with self.factory() as s:
            if stage == 'market':
                securities = {r.ts_code: r for r in s.scalars(select(Security).where(Security.ts_code.in_(self.selection.symbols)))}
                calendars = s.scalars(calendar_query(start, as_of)).all()
                available = {r.exchange for r in calendars}
                for symbol in self.selection.symbols:
                    security = securities.get(symbol)
                    if security is None or security.list_date is None:
                        raise ValueError('stock listing evidence missing')
                    exchange = calendar_exchange(security.exchange, available)
                    expected = {r.cal_date for r in calendars if r.exchange == exchange and r.is_open and r.cal_date >= security.list_date}
                    for dataset, model in MARKET.items():
                        existing = set(s.scalars(select(model.trade_date).where(model.ts_code == symbol,
                            model.trade_date >= start, model.trade_date <= as_of)))
                        missing = expected-existing
                        if missing:
                            plans.append(plan_for(dataset, {'ts_code': symbol, 'start_date': min(missing).strftime('%Y%m%d'),
                                'end_date': max(missing).strftime('%Y%m%d')}))
            elif stage == 'financial':
                rows = s.scalars(financial_rows_query(DisclosureSchedule, self.selection.symbols, start=start).where(
                    DisclosureSchedule.actual_date <= as_of,
                    DisclosureSchedule.actual_date.is_not(None))).all()
                for disclosure in rows:
                    for dataset, model in FINANCIAL.items():
                        inventory = s.scalars(select(model).where(model.ts_code == disclosure.ts_code, model.end_date == disclosure.end_date)).all()
                        fields = FIELD_REGISTRY[dataset] if backfill_card_fields else ()
                        if dataset == Dataset.CASHFLOW and legacy_cashflow:
                            fields = ()
                        if not financial_requirement_met(inventory, as_of, disclosure.actual_date, dataset, fields):
                            plans.append(replace(plan_for(dataset, {'ts_code': disclosure.ts_code, 'period': disclosure.end_date.strftime('%Y%m%d')}),
                                required_visible_since=disclosure.actual_date.isoformat()))
            else:
                raise ValueError('invalid stage')
        return tuple(plans)

    def plans(self, as_of: date):
        return tuple(p for stage in ('actions', 'market', 'financial') for p in self.stage_plans(stage, as_of))
