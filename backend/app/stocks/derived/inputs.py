from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException
from typing import Iterable, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    AdjustmentFactor,
    BalanceSheet,
    BusinessSegment,
    CashflowStatement,
    Classification,
    ClassificationMember,
    ContentDocument,
    DailyBasic,
    DailyPrice,
    Dividend,
    FinancialIndicator,
    IncomeStatement,
    MacroObservation,
    Repurchase,
    Security,
    ShareUnlock,
    TopHolder,
    TradeCalendar,
)
from app.shared.fingerprints import canonical_source_hash
from app.stocks.dataset_query import (announced_rows_query, calendar_query, daily_rows_query, financial_rows_query,
    latest_price_with_basic, memberships_query, securities_query)
from app.ingestion.normalizers import parse_date
from app.stocks.derived.registry import FORMULAS
from app.shared.stock_mapping import StockMappingPolicy, finite_decimal


@dataclass(frozen=True)
class FinancialPoint:
    end_date: date
    visible_at: date | None
    report_type: str | None
    values: Mapping[str, object]
    ts_code: str | None = None
    source_table: str | None = None
    source_record_key: str | None = None

    @property
    def announcement_date(self) -> date | None:
        return parse_date(self.values.get("ann_date"))

    def disclosed_by(self, as_of: date) -> bool:
        return self.visible_at is not None and self.visible_at <= as_of and (self.announcement_date or date.min) <= as_of


def metric_report_revisions(rows, *, indicator=False, as_of=None):
    """每证券、报告期的合并累计及单季最新版本，同级多行交由使用者核对。"""
    groups = {}
    for row in rows:
        if as_of is not None and row.ann_date is not None and row.ann_date > as_of:
            continue
        report = row.report_type
        if report not in {'1', '2', '3', '4', '5'} and not (indicator and not report):
            continue
        kind = 'quarter' if report in _SINGLE_QUARTER_REPORT_TYPES else 'cumulative'
        key = row.ts_code, row.end_date, kind
        rank = (report != '5', row.f_ann_date or row.ann_date, str(row.update_flag or ''),
            {'3': 2, '4': 2, '1': 1, '2': 1}.get(report, 0), row.ann_date or date.min)
        if key not in groups or groups[key][0] < rank:
            groups[key] = rank, [row]
        elif groups[key][0] == rank:
            groups[key][1].append(row)
    return {key: items for key, (_, items) in groups.items()}


CARD_REPORT_FIELDS = {
    "income": ("basic_eps", "n_income_attr_p"),
    "balance": ("money_cap", "st_borr", "lt_borr", "st_bonds_payable", "bond_payable",
                "lease_liab", "non_cur_liab_due_1y", "total_share"),
    "indicator": ("eps", "bps", "q_eps", "interestdebt"),
}


def load_metric_card_inputs(session: Session, symbols: Iterable[str], as_of: date, *, dividend_years=None) -> dict[str, dict]:
    """八卡独立输入，沿用供应商原单位；由发布者提供一致读事务。"""
    from app.stocks.derived.metric_dividends import load_dividend_years

    codes = tuple(sorted(set(symbols)))
    if not codes:
        return {}
    from app.stocks.financial_supply import metric_card_originals
    originals = {"income": [], "balance": [], "indicator": []}
    for kind, original in metric_card_originals(session, codes, as_of):
        originals[kind].append(original)
    dividends = dividend_years if dividend_years is not None else load_dividend_years(session, codes, as_of, history_years=None)
    result = {code: dict(symbol=code, asOf=as_of.isoformat(), financials=[],
        dividends=dividends[code], quote=dict.fromkeys(
            ('tradeDate', 'close', 'totalShare', 'totalMv', 'peTtm', 'pb', 'basicSource', 'priceSource'))) for code in codes}
    for price, basic in session.execute(latest_price_with_basic(codes, as_of)):
        result[price.ts_code]['quote'] = dict(tradeDate=price.trade_date.isoformat(),
            close=str(price.close) if price.close is not None else None,
            totalShare=(basic.data or {}).get('total_share') if basic else None,
            totalMv=str(basic.total_mv) if basic and basic.total_mv is not None else None,
            peTtm=(basic.data or {}).get('pe_ttm') if basic else None,
            pb=str(basic.pb) if basic and basic.pb is not None else None,
            priceSource=f'core.daily_price:{price.id}', basicSource=f'core.daily_basic:{basic.id}' if basic else None)

    for model, table in ((IncomeStatement, 'income'), (BalanceSheet, 'balance'), (FinancialIndicator, 'indicator')):
        fields = CARD_REPORT_FIELDS[table]
        disclosed = func.coalesce(model.f_ann_date, model.ann_date)
        rows = session.scalars(financial_rows_query(model, codes, end=as_of)
            .where(disclosed <= as_of).order_by(model.id))
        groups = metric_report_revisions([*rows, *originals[table]], indicator=table == 'indicator', as_of=as_of)
        for (code, period, kind), revisions in sorted(groups.items()):
            row = revisions[0]
            values, conflicts = {}, []
            for field in fields:
                observed = [r.data[field] for r in revisions if field in (r.data or {})]
                # Raw 精确 Decimal 与 Core JSON 数值按十进制核对；窄合同省略字段不构成冲突。
                compared = [finite_decimal(v) for v in observed]
                if any(item != compared[0] for item in compared):
                    conflicts.append(field)
                else:
                    value = observed[0] if observed else None
                    values[field] = str(value) if isinstance(value, Decimal) else value
            values['comp_type'] = row.comp_type
            source_ids = [r.source_id if hasattr(r, 'source_id') else f'core.{model.__tablename__}:{r.id}' for r in revisions]
            result[code]['financials'].append(dict(table=table, kind=kind, period=period.isoformat(),
                announcedAt=(row.f_ann_date or row.ann_date).isoformat(), reportType=row.report_type,
                updateFlag=row.update_flag, sourceId=source_ids[0], sourceIds=source_ids,
                conflicts=conflicts, values=values))
    return result


@dataclass(frozen=True)
class InputSource:
    source_table: str
    source_record_key: str
    value: object

    @property
    def content_hash(self) -> str:
        return canonical_source_hash(
            self.value,
            self.source_table,
            self.source_record_key,
        )


@dataclass(frozen=True)
class FormulaInput:
    name: str
    value: object
    source: InputSource


@dataclass(frozen=True)
class SecurityFormulaInputs:
    values: Mapping[str, object]
    sources: Mapping[str, InputSource]
    risk_values: Mapping[str, object]


@dataclass(frozen=True)
class CoreSnapshot:
    as_of: date
    securities: tuple[Security, ...]
    daily_prices: tuple[DailyPrice, ...]
    daily_basics: tuple[DailyBasic, ...]
    adjustment_factors: tuple[AdjustmentFactor, ...]
    security_inputs: Mapping[str, SecurityFormulaInputs]
    missing_data: tuple[dict[str, object], ...]
    financial_points: tuple[FinancialPoint, ...]
    corporate_actions: tuple[object, ...]
    classifications: tuple[Classification, ...]
    macro_observations: tuple[MacroObservation, ...]
    content_documents: tuple[ContentDocument, ...]
    classification_members: tuple[ClassificationMember, ...] = ()
    trade_calendar: tuple[TradeCalendar, ...] = ()
    classification_history: tuple[ClassificationMember, ...] = ()
    missing_financial_disclosure_count: int = 0


_REPORT_TYPE_ORDER = (
    "1", "4", "5", "11", "2", "3", "6", "9", "10", "12", "7", "8",
)
_SINGLE_QUARTER_REPORT_TYPES = frozenset({"2", "3", "7", "8"})


def _report_type_rank(value: str | None) -> tuple[int, str]:
    normalized = value or ""
    try:
        return _REPORT_TYPE_ORDER.index(normalized), normalized
    except ValueError:
        return len(_REPORT_TYPE_ORDER), normalized


def ttm_value(
    current_ytd: Decimal | None,
    prior_annual: Decimal | None,
    prior_ytd: Decimal | None,
) -> Decimal | None:
    if current_ytd is None or prior_annual is None or prior_ytd is None:
        return None
    return current_ytd + prior_annual - prior_ytd


def visible_financial_rows(
    rows: Iterable[FinancialPoint],
    as_of: date,
) -> tuple[FinancialPoint, ...]:
    visible = [
        row
        for row in rows
        if row.disclosed_by(as_of)
    ]
    def revision_rank(point: FinancialPoint) -> tuple[int, date, str]:
        raw = point.values.get("update_flag")
        try:
            update_flag = int(str(raw)) if raw is not None else -1
        except ValueError:
            update_flag = -1
        return update_flag, point.announcement_date or date.min, point.source_record_key or ""

    latest: dict[tuple[date, str | None], FinancialPoint] = {}
    for row in sorted(
        visible,
        key=lambda item: (
            item.visible_at or date.min,
            *revision_rank(item),
        ),
    ):
        latest[(row.end_date, row.report_type)] = row
    return tuple(
        sorted(
            latest.values(),
            key=lambda item: (item.end_date, _report_type_rank(item.report_type)),
        )
    )


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _sum_decimal(values: Iterable[object]) -> Decimal | None:
    converted = [_decimal(value) for value in values]
    present = [value for value in converted if value is not None]
    return sum(present, Decimal("0")) if present else None


def _row_data(row: object) -> dict[str, object]:
    data = dict(getattr(row, "data", {}) or {})
    for name in (
        "ann_date",
        "f_ann_date",
        "end_date",
        "report_type",
        "comp_type",
        "update_flag",
        "cash_div",
        "cash_div_tax",
        "proc",
        "amount",
        "hold_amount",
    ):
        value = getattr(row, name, None)
        if value is not None:
            data.setdefault(name, value)
    return data


def _record_key(row: object) -> str:
    ts_code = str(getattr(row, "ts_code", ""))
    if hasattr(row, "trade_date"):
        return f"{ts_code}:{getattr(row, 'trade_date').isoformat()}"
    end_date = getattr(row, "end_date", None)
    ann_date = getattr(row, "ann_date", None)
    report_type = getattr(row, "report_type", None)
    update_flag = getattr(row, "update_flag", None)
    return ":".join(
        (
            ts_code,
            end_date.isoformat() if isinstance(end_date, date) else "",
            report_type or "",
            ann_date.isoformat() if isinstance(ann_date, date) else "",
            update_flag or "",
        )
    )


def _value(point: FinancialPoint | None, *names: str) -> object:
    if point is None:
        return None
    for name in names:
        if point.values.get(name) is not None:
            return point.values[name]
    return None


def _latest(points: Iterable[FinancialPoint]) -> FinancialPoint | None:
    candidates = tuple(points)
    if not candidates:
        return None
    latest_date = max(point.end_date for point in candidates)
    latest_period = [point for point in candidates if point.end_date == latest_date]
    preferred_report_type = min(
        latest_period,
        key=lambda point: _report_type_rank(point.report_type),
    ).report_type
    return max(
        (point for point in latest_period if point.report_type == preferred_report_type),
        key=lambda point: (point.visible_at or date.min, point.source_record_key or ""),
    )


def _ttm(
    points: tuple[FinancialPoint, ...],
    names: tuple[str, ...],
) -> tuple[Decimal | None, tuple[FinancialPoint, ...]]:
    if not points:
        return None, ()
    ordered = sorted(
        points,
        key=lambda item: (item.end_date, _report_type_rank(item.report_type)),
    )
    current = _latest(ordered)
    assert current is not None
    current_value = _decimal(_value(current, *names))
    if (
        current.end_date.month == 12
        and current.report_type in _SINGLE_QUARTER_REPORT_TYPES
    ):
        return None, (current,)
    if current.end_date.month == 12:
        return current_value, (current,)
    prior_annual = next(
        (
            point
            for point in reversed(ordered)
            if point.end_date.year == current.end_date.year - 1
            and point.end_date.month == 12
            and point.report_type == current.report_type
        ),
        None,
    )
    prior_ytd = next(
        (
            point
            for point in reversed(ordered)
            if point.end_date.year == current.end_date.year - 1
            and point.end_date.month == current.end_date.month
            and point.end_date.day == current.end_date.day
            and point.report_type == current.report_type
        ),
        None,
    )
    value = ttm_value(
        current_value,
        _decimal(_value(prior_annual, *names)),
        _decimal(_value(prior_ytd, *names)),
    )
    source_points = tuple(
        point for point in (current, prior_annual, prior_ytd) if point is not None
    )
    return value, source_points


def _years_before(value: date, years: int) -> date:
    year = value.year - years
    return date(year, value.month, min(value.day, monthrange(year, value.month)[1]))


def _previous_comparable(
    points: tuple[FinancialPoint, ...] | list[FinancialPoint],
    current: FinancialPoint | None,
) -> FinancialPoint | None:
    if current is None:
        return None
    target_year = current.end_date.year - 1
    return next(
        (
            point
            for point in reversed(sorted(points, key=lambda item: item.end_date))
            if point.end_date.year == target_year
            and point.end_date.month == current.end_date.month
            and point.end_date.day == current.end_date.day
            and point.report_type == current.report_type
        ),
        None,
    )


class CoreSnapshotRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def load(
        self,
        as_of: date,
        symbols: tuple[str, ...] = (),
        *,
        market_history: bool = True,
    ) -> CoreSnapshot:
        history_start = _years_before(as_of, 3)
        with self._session_factory() as session:
            securities = tuple(
                row
                for row in session.scalars(securities_query()).all()
                if (row.list_date is None or row.list_date <= as_of)
                and (row.delist_date is None or row.delist_date > as_of)
                and not (row.delist_date is None and row.status in {"D", "DELISTED", "退市"})
            )
            codes = tuple(row.ts_code for row in securities)

            def market_rows(model):
                statement = daily_rows_query(model, history_start, as_of, codes, latest_only=not market_history)
                return tuple(session.scalars(statement.order_by(model.ts_code, model.trade_date)).all())

            prices = market_rows(DailyPrice)
            basics = market_rows(DailyBasic)
            factors = market_rows(AdjustmentFactor)
            formula_codes = tuple(sorted(set(symbols))) or codes
            financial_by_table, financial_points, missing_disclosures = self._load_financial(
                session,
                formula_codes,
                as_of,
            )
            actions = self._load_actions(session, formula_codes, as_of)
            from app.stocks.derived.metric_dividends import load_dividend_years
            paid_cash_records = load_dividend_years(session, formula_codes, as_of, history_years=None)
            top_holders = tuple(
                session.scalars(
                    select(TopHolder).where(
                        TopHolder.ts_code.in_(formula_codes),
                        TopHolder.end_date <= as_of,
                        TopHolder.ann_date.is_not(None),
                        TopHolder.ann_date <= as_of,
                    )
                    .order_by(TopHolder.ts_code, TopHolder.end_date, TopHolder.holder_name)
                ).all()
            )
            inputs, missing_data = self._assemble_inputs(
                securities,
                prices,
                basics,
                financial_by_table,
                actions,
                top_holders,
                as_of,
                formula_codes,
                paid_cash_records,
            )
            cutoff = datetime.combine(as_of, datetime.max.time(), timezone.utc)
            from app.macro.api.observations import stored_observations
            macro = tuple(sorted(stored_observations(session, None, cutoff),
                                 key=lambda row: (row.series_id, row.observed_at)))
            documents = tuple(
                session.scalars(
                    select(ContentDocument)
                    .where(ContentDocument.published_at <= cutoff)
                    .order_by(ContentDocument.published_at)
                ).all()
            )
            classifications = tuple(
                session.scalars(select(Classification).order_by(Classification.id)).all()
            )
            membership_history = tuple(
                session.scalars(
                    memberships_query(codes)
                    .order_by(ClassificationMember.classification_id, ClassificationMember.ts_code)
                ).all()
            )
            members = tuple(row for row in membership_history
                            if row.effective_from <= as_of and (row.effective_to is None or row.effective_to >= as_of))
            calendar = tuple(session.scalars(calendar_query(history_start, as_of)).all())
            return CoreSnapshot(
                as_of=as_of,
                securities=securities,
                daily_prices=prices,
                daily_basics=basics,
                adjustment_factors=factors,
                security_inputs=inputs,
                missing_data=tuple(missing_data),
                financial_points=financial_points,
                corporate_actions=actions,
                classifications=classifications,
                macro_observations=macro,
                content_documents=documents,
                classification_members=members,
                trade_calendar=calendar,
                classification_history=membership_history,
                missing_financial_disclosure_count=missing_disclosures,
            )

    def _load_financial(
        self,
        session: Session,
        codes: tuple[str, ...],
        as_of: date,
    ) -> tuple[dict[tuple[str, str], tuple[FinancialPoint, ...]], tuple[FinancialPoint, ...], int]:
        models = (
            (IncomeStatement, "core.income_statement"),
            (BalanceSheet, "core.balance_sheet"),
            (CashflowStatement, "core.cashflow_statement"),
            (FinancialIndicator, "core.financial_indicator"),
            (BusinessSegment, "core.business_segment"),
        )
        grouped: dict[tuple[str, str], tuple[FinancialPoint, ...]] = {}
        all_points: list[FinancialPoint] = []
        missing_disclosures = 0
        for model, source_table in models:
            rows = session.scalars(
                financial_rows_query(model, codes)
            ).all()
            by_code: dict[str, list[FinancialPoint]] = {}
            for row in rows:
                visible_at = getattr(row, "f_ann_date", None) or getattr(
                    row, "ann_date", None
                )
                if isinstance(row, BusinessSegment):
                    visible_at = row.end_date
                if visible_at is None:
                    # 只保留诊断计数，未知披露记录不进入财务点或公式输入。
                    # 历史回溯不报告该时点尚不存在的未来报告期。
                    if row.end_date <= as_of:
                        missing_disclosures += 1
                    continue
                if visible_at > as_of:
                    continue
                point = FinancialPoint(
                    end_date=getattr(row, "end_date"),
                    visible_at=visible_at,
                    report_type=getattr(row, "report_type", None),
                    values=_row_data(row),
                    ts_code=getattr(row, "ts_code", None),
                    source_table=source_table,
                    source_record_key=_record_key(row),
                )
                by_code.setdefault(point.ts_code or "", []).append(point)
                all_points.append(point)
            for code, points in by_code.items():
                grouped[(code, source_table)] = visible_financial_rows(points, as_of)
        return grouped, tuple(
            sorted(
                all_points,
                key=lambda point: (
                    point.ts_code or "",
                    point.source_table or "",
                    point.end_date,
                    point.visible_at or date.min,
                ),
            )
        ), missing_disclosures

    def _load_actions(
        self,
        session: Session,
        codes: tuple[str, ...],
        as_of: date,
    ) -> tuple[object, ...]:
        actions: list[object] = []
        dividend_rows: list[Dividend] = []
        for model in (Dividend, Repurchase, ShareUnlock):
            rows = session.scalars(announced_rows_query(model, codes, end=as_of)).all()
            for row in rows:
                if isinstance(row, Dividend):
                    dividend_rows.append(row)
                    continue
                if isinstance(row, ShareUnlock):
                    if row.float_date is None or row.float_date > as_of:
                        continue
                actions.append(row)
        from app.portfolio.api.dividends import select_legacy_dividend_rows
        actions.extend(select_legacy_dividend_rows(dividend_rows, as_of))
        return tuple(
            sorted(
                actions,
                key=lambda row: (
                    getattr(row, "ts_code", ""),
                    getattr(row, "end_date", None) or date.min,
                    getattr(row, "ann_date", None) or date.min,
                ),
            )
        )

    def _assemble_inputs(
        self,
        securities: tuple[Security, ...],
        prices: tuple[DailyPrice, ...],
        basics: tuple[DailyBasic, ...],
        financial_by_table: Mapping[tuple[str, str], tuple[FinancialPoint, ...]],
        actions: tuple[object, ...],
        top_holders: tuple[TopHolder, ...],
        as_of: date,
        formula_codes: tuple[str, ...],
        paid_cash_records=None,
    ) -> tuple[dict[str, SecurityFormulaInputs], list[dict[str, object]]]:
        mapping = StockMappingPolicy.load()
        by_price: dict[str, list[DailyPrice]] = {}
        for row in prices:
            by_price.setdefault(row.ts_code, []).append(row)
        by_basic: dict[str, list[DailyBasic]] = {}
        for row in basics:
            by_basic.setdefault(row.ts_code, []).append(row)
        by_action: dict[str, list[object]] = {}
        for row in actions:
            by_action.setdefault(getattr(row, "ts_code", ""), []).append(row)
        by_holder: dict[str, list[TopHolder]] = {}
        for row in top_holders:
            by_holder.setdefault(row.ts_code, []).append(row)
        securities_by_code = {row.ts_code: row for row in securities}
        result: dict[str, SecurityFormulaInputs] = {}
        missing_data: list[dict[str, object]] = []
        for code in formula_codes:
            security = securities_by_code.get(code)
            if security is None:
                continue
            values: dict[str, object] = {}
            sources: dict[str, InputSource] = {}

            def set_input(
                name: str,
                value: object,
                source_table: str,
                source_record_key: str,
            ) -> None:
                values[name] = value
                sources[name] = InputSource(source_table, source_record_key, value)

            price_rows = by_price.get(code, [])
            basic_rows = by_basic.get(code, [])
            latest_price = max(price_rows, key=lambda row: row.trade_date, default=None)
            latest_basic = max(basic_rows, key=lambda row: row.trade_date, default=None)
            security_key = f"{code}:security"
            set_input("industry", security.data.get("industry"), "core.security", security_key)
            set_input("as_of_year", as_of.year, "core.security", security_key)
            set_input(
                "close",
                latest_price.close if latest_price else None,
                "core.daily_price",
                _record_key(latest_price) if latest_price else f"{code}:missing",
            )
            for name in ("pe", "pb", "float_shares", "dv_ratio", "dv_ttm"):
                value: object = None
                if latest_basic:
                    if name == "float_shares":
                        value = latest_basic.data.get("float_share")
                    else:
                        value = getattr(latest_basic, name, None)
                        if value is None:
                            value = latest_basic.data.get(name)
                set_input(
                    name,
                    value,
                    "core.daily_basic",
                    _record_key(latest_basic) if latest_basic else f"{code}:missing",
                )
            set_input(
                "pe_history_3y",
                tuple(
                    row.pe
                    for row in basic_rows
                    if row.pe is not None
                    and row.trade_date >= _years_before(as_of, 3)
                ),
                "core.daily_basic",
                f"{code}:pe-history-3y:{as_of.isoformat()}",
            )
            set_input(
                "pb_history_3y",
                tuple(
                    row.pb
                    for row in basic_rows
                    if row.pb is not None
                    and row.trade_date >= _years_before(as_of, 3)
                ),
                "core.daily_basic",
                f"{code}:pb-history-3y:{as_of.isoformat()}",
            )

            income = financial_by_table.get((code, "core.income_statement"), ())
            balance = financial_by_table.get((code, "core.balance_sheet"), ())
            cashflow = financial_by_table.get((code, "core.cashflow_statement"), ())
            indicators = financial_by_table.get((code, "core.financial_indicator"), ())
            latest_indicator = _latest(indicators)
            for name in ("roe", "grossprofit_margin", "netprofit_margin"):
                set_input(
                    name,
                    _decimal(_value(latest_indicator, name)),
                    "core.financial_indicator",
                    latest_indicator.source_record_key if latest_indicator else f"{code}:missing:{name}",
                )
            for table, rows in (
                ("core.income_statement", income),
                ("core.balance_sheet", balance),
                ("core.cashflow_statement", cashflow),
                ("core.financial_indicator", indicators),
            ):
                if not rows:
                    missing_data.append({"source": table, "symbol": code})

            def set_ttm(
                name: str,
                rows: tuple[FinancialPoint, ...],
                names: tuple[str, ...],
                table: str,
            ) -> Decimal | None:
                value, source_points = _ttm(rows, names)
                key = ":".join(
                    point.source_record_key or "missing" for point in source_points
                ) or f"{code}:missing:{name}"
                set_input(name, value, table, f"ttm:{key}")
                return value

            parent_profit_ttm = set_ttm(
                "parent_profit_ttm",
                income,
                ("n_income_attr_p", "n_income", "total_profit"),
                "core.income_statement",
            )
            revenue_ttm = set_ttm(
                "revenue_ttm",
                income,
                ("total_revenue", "revenue"),
                "core.income_statement",
            )
            cfo_ttm = set_ttm(
                "cfo_ttm",
                cashflow,
                ("n_cashflow_act",),
                "core.cashflow_statement",
            )
            capex_ttm = set_ttm(
                "capex_ttm",
                cashflow,
                ("c_pay_acq_const_fiolta",),
                "core.cashflow_statement",
            )
            nopat_point = _latest(income)
            ebit = _decimal(_value(nopat_point, "ebit"))
            tax_rate = _decimal(_value(_latest(indicators), "tax_to_ebt"))
            nopat = ebit * (Decimal("1") - tax_rate) if ebit is not None and tax_rate is not None else ebit
            set_input(
                "nopat",
                nopat,
                "core.income_statement",
                nopat_point.source_record_key if nopat_point else f"{code}:missing:nopat",
            )
            invested_capital = _value(_latest(indicators), "invest_capital")
            if invested_capital is None:
                invested_capital = _value(_latest(balance), "total_assets")
            set_input(
                "invested_capital",
                invested_capital,
                "core.financial_indicator" if _latest(indicators) else "core.balance_sheet",
                (_latest(indicators) or _latest(balance)).source_record_key
                if (_latest(indicators) or _latest(balance))
                else f"{code}:missing:invested-capital",
            )

            income_points = sorted(income, key=lambda point: point.end_date)
            latest_income = income_points[-1] if income_points else None
            previous_income = _previous_comparable(income_points, latest_income)
            latest_cashflow = _latest(cashflow)
            previous_cashflow = _previous_comparable(cashflow, latest_cashflow)
            for name, point, names in (
                ("profit", latest_income, ("n_income_attr_p", "n_income", "total_profit")),
                ("previous_profit", previous_income, ("n_income_attr_p", "n_income", "total_profit")),
                ("revenue", latest_income, ("total_revenue", "revenue")),
                ("previous_revenue", previous_income, ("total_revenue", "revenue")),
                ("cfo", latest_cashflow, ("n_cashflow_act",)),
                ("previous_cfo", previous_cashflow, ("n_cashflow_act",)),
            ):
                set_input(
                    name,
                    _value(point, *names),
                    point.source_table if point else "core.income_statement",
                    point.source_record_key if point else f"{code}:missing:{name}",
                )
            set_input("parent_profit", parent_profit_ttm, "core.income_statement", f"{code}:parent-profit-ttm")

            balance_point = _latest(balance)
            cash = _decimal(_value(balance_point, "money_cap"))
            inventories = _decimal(_value(balance_point, "inventories"))
            goodwill = _decimal(_value(balance_point, "goodwill"))
            assets = _decimal(_value(balance_point, "total_assets"))
            equity = _decimal(_value(balance_point, "total_hldr_eqy_exc_min_int"))
            debt = _sum_decimal(
                _value(balance_point, field)
                for field in (
                    "st_borr",
                    "lt_borr",
                    "st_bonds_payable",
                    "non_cur_liab_due_1y",
                    "bond_payable",
                    "lease_liab",
                )
            )
            net_debt_value = _decimal(_value(_latest(indicators), "netdebt"))
            if net_debt_value is None and debt is not None and cash is not None:
                net_debt_value = debt - cash
            set_input("total_debt", debt, "core.balance_sheet", _record_key(balance_point) if balance_point else f"{code}:missing")
            set_input("cash", cash, "core.balance_sheet", _record_key(balance_point) if balance_point else f"{code}:missing")
            set_input("cash_dividend", None, "core.dividend", f"{code}:missing:cash-dividend")
            set_input("dividends", None, "core.dividend", f"{code}:missing:dividends")
            set_input("buybacks", None, "core.repurchase", f"{code}:missing:buybacks")
            set_input("equity_financing_inflow", _value(_latest(cashflow), "c_recp_cap_contrib"), "core.cashflow_statement", _latest(cashflow).source_record_key if _latest(cashflow) else f"{code}:missing")
            from app.stocks.shareholder_returns import annual_distributions
            records = (paid_cash_records or {}).get(code, {})
            profits = {str(point.end_date.year): _value(point, 'n_income_attr_p') for point in income
                       if point.end_date.month == 12 and point.end_date.day == 31}
            distributions = {r['year']: r for r in annual_distributions(records, profits, as_of)}
            fiscal = distributions.get(str(as_of.year - 1), {})
            source_key = f"{code}:paid-cash-records-v1:{as_of.isoformat()}:" + canonical_source_hash(records, 'core.dividend', code)
            set_input('cash_dividend', fiscal.get('cashAmount'), 'core.dividend', source_key)
            set_input('dividends', fiscal.get('cashAmount'), 'core.dividend', source_key)
            set_input('parent_profit', fiscal.get('parentProfit'), 'core.income_statement', f'{code}:parent-profit:{as_of.year - 1}')
            repurchase_rows = [row for row in by_action.get(code, []) if isinstance(row, Repurchase)]
            if repurchase_rows:
                set_input("buybacks", _sum_decimal((_row_data(row).get("amount") for row in repurchase_rows)), "core.repurchase", f"{code}:buybacks:{as_of.isoformat()}")
            dividend_history = tuple(distributions.get(str(y), {}).get('dps') for y in range(as_of.year - 3, as_of.year))
            # 预测必须使用连续三年有效实付历史，不跳过缺年另找样本。
            set_input('dividend_history', dividend_history, 'core.dividend', source_key)
            set_input('dividend_fiscal_years', records, 'core.dividend', source_key)
            set_input('latest_dps', fiscal.get('dps'), 'core.dividend', source_key)
            holder_rows = by_holder.get(code, [])
            latest_holder_period = max((row.end_date for row in holder_rows), default=None)
            if latest_holder_period is not None:
                holder_rows = [row for row in holder_rows if row.end_date == latest_holder_period]
            # 同一报告期同名股东只取当时已披露的最新修订，不跨期累加。
            latest_holders = {}
            invalid_holders = False
            invalid_holder_name = False
            holder_groups = {}
            for row in holder_rows:
                normalized_name = row.holder_name.strip() if isinstance(row.holder_name, str) else ""
                if not normalized_name:
                    invalid_holders = True
                    invalid_holder_name = True
                    continue
                holder_groups.setdefault(normalized_name, []).append(row)
            for name, rows in holder_groups.items():
                latest_ann = max(row.ann_date for row in rows)
                current = [row for row in rows if row.ann_date == latest_ann]
                latest_holders[name] = current[0]
                if len({(row.hold_amount, row.holder_type) for row in current}) > 1:
                    invalid_holders = True
            identified = [row for row in latest_holders.values() if mapping.holder_kind(row.holder_type) == "included"]
            unknown_count = sum(mapping.holder_kind(row.holder_type) == "unknown" for row in latest_holders.values())
            amounts = [finite_decimal(row.hold_amount) for row in identified]
            institution_shares = None
            if latest_holders and not invalid_holders and all(value is not None and value >= 0 for value in amounts):
                if identified or not unknown_count:
                    try:
                        institution_shares = sum(amounts, Decimal("0"))
                    except DecimalException:
                        institution_shares = None
            period_basic = max((row for row in basic_rows if latest_holder_period is not None
                and row.trade_date <= latest_holder_period), key=lambda row: row.trade_date, default=None)
            capital = None
            if period_basic is not None and (latest_holder_period - period_basic.trade_date).days <= mapping.institution.maxDenominatorLagDays:
                capital = finite_decimal((period_basic.data or {}).get("total_share"))
                try:
                    capital = capital * Decimal("10000") if capital is not None else None
                except DecimalException:
                    capital = None
            calculation_status = (
                "invalid_holder_name" if invalid_holder_name else
                "holder_revision_conflict" if invalid_holders else
                "missing_holder_records" if not latest_holders else
                "unknown_holder_types" if not identified and unknown_count else
                "invalid_or_missing_holder_amount" if institution_shares is None else
                "invalid_report_capital" if capital is None or capital <= 0 else
                "invalid_holder_amount" if institution_shares > capital else "valid_partial"
            )
            holder_key = canonical_source_hash(tuple((row.holder_name, row.holder_type, row.hold_amount, row.ann_date)
                for row in sorted(latest_holders.values(), key=lambda item: item.holder_name)), "core.top_holder", code)
            set_input("institution_shares", institution_shares, "core.top_holder",
                f"{code}:institution:{latest_holder_period}:policy={mapping.fingerprint}:rows={holder_key}")
            set_input("institution_total_shares", capital, "core.daily_basic",
                f"{_record_key(period_basic) if period_basic else code + ':missing'}:total_share:shares")
            missing_data.append({"source": "core.top_holder", "symbol": code, "reason": "institution_partial_coverage",
                "reportPeriod": latest_holder_period.isoformat() if latest_holder_period else None,
                "identifiedCount": len(identified), "unknownTypeCount": unknown_count,
                "calculationStatus": calculation_status,
                "policyVersion": mapping.policyVersion, "policyHash": mapping.fingerprint})
            set_input("net_debt", net_debt_value, "core.financial_indicator" if _latest(indicators) else "core.balance_sheet", f"{code}:net-debt:{as_of.isoformat()}")

            risk_values = {
                "inventory_ratio": inventories / revenue_ttm if inventories is not None and revenue_ttm not in (None, Decimal("0")) else None,
                "goodwill_ratio": goodwill / assets if goodwill is not None and assets not in (None, Decimal("0")) else None,
                "net_debt_ratio": net_debt_value / equity if net_debt_value is not None and equity not in (None, Decimal("0")) else None,
                "cashflow_dividend_ratio": cfo_ttm / _decimal(values.get("cash_dividend")) if cfo_ttm is not None and _decimal(values.get("cash_dividend")) not in (None, Decimal("0")) else None,
            }
            result[code] = SecurityFormulaInputs(values, sources, risk_values)
        for metric_id, definition in FORMULAS.items():
            if not definition.inputs:
                continue
        return result, missing_data
