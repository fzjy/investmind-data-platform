"""证券主表、交易日历、分类关系、财报版本与分红的只读 HTTP 合同。"""
import base64
from collections import defaultdict
from datetime import date
from hashlib import sha256
import json
import re
from typing import Annotated, Generic, Literal, TypeVar

from fastapi import APIRouter, Query
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from sqlalchemy import select, text

from app.connectors.tushare.strategies import Dataset, FIELD_REGISTRY
from app.db.models import (BalanceSheet, CashflowStatement, Classification, ClassificationMember,
    DisclosureSchedule, FinancialExpress, FinancialForecast, FinancialIndicator, IncomeStatement,
    Repurchase, Security, SecurityNameHistory, ShareUnlock, TradeCalendar)
from app.portfolio.api.dividend_facts import security_aliases
from app.shared.errors import DataServiceError
from app.shared.stock_mapping import finite_decimal
from app.stocks.dataset_query import (
    announced_rows_query, calendar_query, financial_rows_query, memberships_query, securities_query,
)
from app.stocks.dividend_facts import dividend_observations
from app.stocks.universes.resolver import STOCK_SYMBOL_PATTERN


def _iso_day(value):
    if isinstance(value, str) and date.fromisoformat(value).isoformat() != value:
        raise ValueError('日期须使用 YYYY-MM-DD')
    return value


Day = Annotated[date, BeforeValidator(_iso_day)]
Symbol = Annotated[str, Field(pattern=STOCK_SYMBOL_PATTERN.pattern)]


class Paging(BaseModel):
    model_config = ConfigDict(extra='forbid')
    limit: int = Field(500, ge=1, le=2000)
    cursor: str | None = Field(None, min_length=1, max_length=512)


class SecuritiesQuery(Paging):
    symbol: Symbol | None = None


class Window(Paging):
    start_date: Day
    end_date: Day

    @model_validator(mode='after')
    def check_order(self):
        if self.start_date > self.end_date:
            raise ValueError('起始日期晚于结束日期')
        return self


class CalendarQuery(Window):
    exchange: str = Field(min_length=1, max_length=16, pattern='^[A-Z0-9]+$')


class SymbolWindow(Window):
    symbol: Symbol


class ReportsQuery(SymbolWindow):
    statement: Literal['income', 'balance', 'cashflow', 'indicator']


class DividendsQuery(SymbolWindow):
    as_of: Day | None = Field(None, description='上海时间该日结束前已捕获的完整字段凭证')


class CompanyEventsQuery(SymbolWindow):
    kind: Literal['forecast', 'express', 'repurchase', 'unlock']


class NamePeriod(BaseModel):
    name: str
    start_date: date | None
    end_date: date | None


class SecurityRow(BaseModel):
    symbol: str
    local_code: str
    name: str
    exchange: str | None
    board: str | None
    list_date: date | None
    delist_date: date | None
    status: str | None
    is_st: bool
    names: list[NamePeriod]
    related_codes: list[str]
    alias_status: Literal['observed', 'unavailable', 'not_applicable']
    alias_source_id: str | None


class CalendarDay(BaseModel):
    exchange: str
    cal_date: date
    is_open: bool
    pretrade_date: date | None


class Membership(BaseModel):
    symbol: str
    classification_id: str
    provider: str
    source: str
    name: str
    effective_from: date
    effective_to: date | None
    weight: str | None
    source_id: str


class Report(BaseModel):
    symbol: str
    end_date: date
    ann_date: date | None
    f_ann_date: date | None
    report_type: str | None
    comp_type: str | None
    update_flag: str | None
    source_id: str
    values: dict[str, str | None]


class Disclosure(BaseModel):
    symbol: str
    end_date: date
    ann_date: date | None
    pre_date: date | None
    actual_date: date | None
    modify_date: date | None
    source_id: str


class CompanyEvent(BaseModel):
    symbol: str
    ann_date: date
    end_date: date | None
    stage: str | None
    float_date: date | None
    exp_date: date | None
    values: dict[str, str | None]
    source_id: str


class DividendVersion(BaseModel):
    ts_code: str
    end_date: str | None
    ann_date: date
    div_proc: str | None
    record_date: str | None
    ex_date: str | None
    pay_date: str | None
    div_listdate: str | None
    imp_ann_date: str | None
    base_date: str | None
    base_share: str | None
    cash_div: str | None
    cash_div_tax: str | None
    stk_div: str | None
    stk_bo_rate: str | None
    stk_co_rate: str | None
    source_id: str
    captured_at: str | None
    source_kind: Literal['core','normalized_capture','verified_original']
    selected_implementation: bool


class Meta(BaseModel):
    dataset: str
    coverage_scope: Literal['stored', 'captured_as_of']
    next_cursor: str | None


class Capture(BaseModel):
    source_id: str
    captured_at: str


class DividendMeta(Meta):
    history_complete: Literal[False]
    evidence_status: Literal['invalid','missing','observed']
    normalized_status: Literal['stored','invalid','not_requested']
    captures: list[Capture]
    missing_announcement_rows: int
    issues: list[dict]


T = TypeVar('T')


class Page(BaseModel, Generic[T]):
    data: list[T]
    meta: Meta


class DividendPage(Page[DividendVersion]):
    meta: DividendMeta


REPORTS = {
    'income': (IncomeStatement, Dataset.INCOME),
    'balance': (BalanceSheet, Dataset.BALANCE_SHEET),
    'cashflow': (CashflowStatement, Dataset.CASHFLOW),
    'indicator': (FinancialIndicator, Dataset.FINANCIAL_INDICATOR),
}

EVENTS = {
    'forecast': (FinancialForecast, ('p_change_min','p_change_max','net_profit_min','net_profit_max')),
    'express': (FinancialExpress, ('revenue','n_income','total_assets','total_hldr_eqy_exc_min_int')),
    'repurchase': (Repurchase, ('vol','amount')),
    'unlock': (ShareUnlock, ('float_share',)),
}


class PagePosition:
    def __init__(self, dataset, query, parse):
        self.dataset = dataset
        self.limit = query.limit
        scope = [dataset, query.model_dump(mode='json', exclude={'limit','cursor'})]
        self.scope = sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
        self.after = None
        if query.cursor:
            try:
                scope_id, key = json.loads(base64.b64decode(query.cursor, altchars=b'-_', validate=True))
                if scope_id != self.scope or type(key) is not str:
                    raise ValueError
                self.after = parse(key)
                if isinstance(self.after, int) and not 0 < self.after <= 2147483647:
                    raise ValueError
                if isinstance(self.after, date) and not query.start_date <= self.after <= query.end_date:
                    raise ValueError
                if dataset == 'stock-securities' and not STOCK_SYMBOL_PATTERN.fullmatch(key):
                    raise ValueError
                if dataset == 'stock-dividends' and not re.fullmatch(
                    r'\d{4}-\d{2}-\d{2}/(?:core\.dividend:\d+|raw\.source_payload:\d+:row:\d+)', key):
                    raise ValueError
            except (ValueError, TypeError, UnicodeError):
                raise DataServiceError('INVALID_REQUEST', '分页游标无效或查询条件已改变') from None

    def fetch(self, session, statement, column, *, scalars=True):
        if self.after is not None:
            statement = statement.where(column > self.after)
        result = session.execute(statement.order_by(None).order_by(column).limit(self.limit + 1))
        return list(result.scalars() if scalars else result)

    def result(self, rows, keys, **meta):
        cursor = None
        if len(rows) > self.limit:
            cursor = base64.urlsafe_b64encode(json.dumps([self.scope, str(keys[self.limit-1])]).encode()).decode()
        return dict(data=rows[:self.limit], meta=dict(dataset=self.dataset,
            coverage_scope='stored', next_cursor=cursor) | meta)


def _numeric_text(value):
    value = finite_decimal(value)
    return format(value, 'f') if value is not None else None


def create_reference_router(factory):
    router = APIRouter()

    @router.get('/stock-securities', response_model=Page[SecurityRow], summary='当前证券主数据及已存曾用名')
    def securities(q: Annotated[SecuritiesQuery, Query()]):
        pos = PagePosition('stock-securities', q, str)
        with factory() as session:
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            rows = pos.fetch(session, securities_query([q.symbol] if q.symbol else None), Security.ts_code)
            names = defaultdict(list)
            symbols = [r.ts_code for r in rows]
            try:
                aliases, mapping = security_aliases(session, symbols)
            except (ValueError, KeyError, TypeError, AttributeError):
                aliases, mapping = {}, None
            name_history = select(SecurityNameHistory).where(SecurityNameHistory.ts_code.in_(symbols)
                ).order_by(SecurityNameHistory.ts_code, SecurityNameHistory.start_date, SecurityNameHistory.id)
            for name in session.scalars(name_history):
                names[name.ts_code].append(dict(name=name.name, start_date=name.start_date, end_date=name.end_date))
            data = [dict(symbol=r.ts_code, local_code=r.symbol, names=names[r.ts_code],
                related_codes=[code for code in aliases.get(r.ts_code, ()) if code != r.ts_code],
                alias_status=('observed' if mapping else 'unavailable') if r.ts_code.endswith('.BJ') else 'not_applicable',
                alias_source_id=f"raw.source_payload:{mapping['unit']['rawId']}" if mapping and r.ts_code.endswith('.BJ') else None,
                **{k:getattr(r,k) for k in ('name','exchange','board','list_date','delist_date','status','is_st')}) for r in rows]
            return pos.result(data, [r.ts_code for r in rows])

    @router.get('/stock-trade-calendar', response_model=Page[CalendarDay], summary='交易所自然日开闭市记录')
    def calendar(q: Annotated[CalendarQuery, Query()]):
        pos = PagePosition('stock-trade-calendar', q, date.fromisoformat)
        with factory() as session:
            rows = pos.fetch(session, calendar_query(q.start_date, q.end_date, q.exchange), TradeCalendar.cal_date)
            return pos.result([{k:getattr(r,k) for k in CalendarDay.model_fields} for r in rows],
                              [r.cal_date for r in rows])

    @router.get('/stock-classification-members', response_model=Page[Membership], summary='区间内生效的证券分类关系')
    def members(q: Annotated[SymbolWindow, Query()]):
        pos = PagePosition('stock-classification-members', q, int)
        statement = memberships_query([q.symbol], start=q.start_date, end=q.end_date
            ).add_columns(Classification).join(Classification, Classification.id == ClassificationMember.classification_id)
        with factory() as session:
            rows = pos.fetch(session, statement, ClassificationMember.id, scalars=False)
            data = [dict(symbol=r.ts_code, classification_id=c.classification_id, provider=c.provider,
                source=c.source, name=c.name, effective_from=r.effective_from, effective_to=r.effective_to,
                weight=_numeric_text(r.weight), source_id=f'core.classification_member:{r.id}') for r,c in rows]
            return pos.result(data, [r.id for r,_ in rows])

    @router.get('/stock-financial-reports', response_model=Page[Report], summary='按报告期查询库存财务报表及版本')
    def reports(q: Annotated[ReportsQuery, Query()]):
        pos = PagePosition('stock-financial-reports', q, int)
        model, dataset = REPORTS[q.statement]
        statement = financial_rows_query(model, [q.symbol], start=q.start_date, end=q.end_date)
        identity = ('end_date','ann_date','f_ann_date','report_type','comp_type','update_flag')
        fields = [k for k in FIELD_REGISTRY[dataset] if k not in (*identity, 'ts_code')]
        # PostgreSQL 直接返回 JSON 数字的文本，跳过驱动将高精度小数解码为 float 的过程。
        statement = statement.with_only_columns(model.id, model.ts_code, *(getattr(model,k) for k in identity),
            *(model.data[k].as_string().label(k) for k in fields))
        with factory() as session:
            rows = pos.fetch(session, statement, model.id, scalars=False)
            data = [dict(symbol=r.ts_code, source_id=f'core.{model.__tablename__}:{r.id}',
                **{k:getattr(r,k) for k in identity}, values={k:_numeric_text(r._mapping[k]) for k in fields})
                for r in rows]
            return pos.result(data, [r.id for r in rows])

    @router.get('/stock-disclosures', response_model=Page[Disclosure], summary='按报告期查询已存财报披露日程')
    def disclosures(q: Annotated[SymbolWindow, Query()]):
        pos = PagePosition('stock-disclosures', q, int)
        statement = financial_rows_query(DisclosureSchedule, [q.symbol], start=q.start_date, end=q.end_date)
        dates = ('end_date', 'ann_date', 'pre_date', 'actual_date', 'modify_date')
        with factory() as session:
            rows = pos.fetch(session, statement, DisclosureSchedule.id)
            data = [dict(symbol=r.ts_code, source_id=f'core.disclosure_schedule:{r.id}',
                         **{k:getattr(r,k) for k in dates}) for r in rows]
            return pos.result(data, [r.id for r in rows])

    @router.get('/stock-company-events', response_model=Page[CompanyEvent], summary='按公告日查询预告、快报、回购及解禁')
    def company_events(q: Annotated[CompanyEventsQuery, Query()]):
        pos = PagePosition('stock-company-events', q, int)
        model, fields = EVENTS[q.kind]
        statement = announced_rows_query(model, [q.symbol], start=q.start_date, end=q.end_date).add_columns(
            *(model.data[k].as_string().label(k) for k in fields))
        with factory() as session:
            rows = pos.fetch(session, statement, model.id, scalars=False)
            data = []
            for result in rows:
                row = result[0]
                values = row.data or {}
                try:
                    expiry = date.fromisoformat(str(values.get('exp_date'))) if q.kind == 'repurchase' else None
                except ValueError:
                    expiry = None
                data.append(dict(symbol=row.ts_code, ann_date=row.ann_date, end_date=getattr(row, 'end_date', None),
                    stage=values.get('type') if q.kind == 'forecast' else getattr(row, 'proc', None),
                    float_date=getattr(row,'float_date',None), exp_date=expiry, values={k:_numeric_text(result._mapping[k]) for k in fields},
                    source_id=f'core.{model.__tablename__}:{row.id}'))
            return pos.result(data, [r[0].id for r in rows])

    @router.get('/stock-dividends', response_model=DividendPage, summary='按公告日查询分红阶段、金额与捕获时点')
    def dividends(q: Annotated[DividendsQuery, Query()]):
        pos = PagePosition('stock-dividends', q, str)
        with factory() as session:
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            data, meta, issues = dividend_observations(session, q.symbol, q.start_date, q.end_date, q.as_of)
        def key(row):
            return row['ann_date'] + '/' + row['source_id']
        if pos.after is not None:
            data = [row for row in data if key(row) > pos.after]
        page_ids = {r['source_id'] for r in data[:q.limit]}
        return pos.result(data, [key(r) for r in data], **meta,
            issues=[issue for issue in issues if page_ids.intersection(issue['sourceIds'])
                    or page_ids.intersection(issue.get('affectedSourceIds', ()))],
            coverage_scope='captured_as_of' if q.as_of is not None else 'stored')

    return router
