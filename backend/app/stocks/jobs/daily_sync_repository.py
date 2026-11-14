from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import exists, select, union
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import (
    DisclosureSchedule,
    PublishedSnapshot,
    Repurchase,
    Security,
    ShareholderCount,
    ShareUnlock,
    TradeCalendar,
)
from app.stocks.universes.models import UniverseSecurityState


@dataclass(frozen=True, slots=True)
class DisclosureTarget:
    symbol: str
    period: date


class DailySyncRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def trading_day(self, business_date: date) -> bool | None:
        statement = select(TradeCalendar.is_open).where(
            TradeCalendar.exchange == "SSE",
            TradeCalendar.cal_date == business_date,
        )
        with self._session_factory() as session:
            return session.scalar(statement)

    def has_active_securities(self) -> bool:
        statement = select(exists().where(Security.status == "L"))
        with self._session_factory() as session:
            return bool(session.scalar(statement))

    def active_symbols(self) -> tuple[str, ...]:
        statement = (
            select(Security.ts_code)
            .where(Security.status == "L")
            .order_by(Security.ts_code)
        )
        with self._session_factory() as session:
            return tuple(session.scalars(statement))

    def published_workbenches(self, allowed_symbols: tuple[str, ...], business_date: date) -> tuple[str, ...]:
        products = ["stock-workbench:" + code for code in allowed_symbols]
        with self._session_factory() as session:
            return tuple(p.partition(":")[2] for p in session.scalars(select(PublishedSnapshot.product)
                .where(PublishedSnapshot.product.in_(products), PublishedSnapshot.as_of < business_date + timedelta(days=1))
                .order_by(PublishedSnapshot.product)))

    def security_states(
        self,
        symbols: tuple[str, ...],
    ) -> dict[str, UniverseSecurityState]:
        if not symbols:
            return {}
        statement = (
            select(
                Security.ts_code,
                Security.status,
                Security.is_st,
                Security.board,
            )
            .where(Security.ts_code.in_(symbols))
            .order_by(Security.ts_code)
        )
        with self._session_factory() as session:
            rows = session.execute(statement).all()
        return {
            ts_code: UniverseSecurityState(
                status=status,
                is_st=is_st,
                board=board,
            )
            for ts_code, status, is_st, board in rows
        }

    def disclosure_targets(
        self,
        start_date: date,
        end_date: date,
        allowed_symbols: tuple[str, ...] | None = None,
    ) -> tuple[DisclosureTarget, ...]:
        if allowed_symbols == ():
            return ()
        symbol_predicates = (
            (DisclosureSchedule.ts_code.in_(allowed_symbols),)
            if allowed_symbols is not None
            else ()
        )
        statement = (
            select(DisclosureSchedule.ts_code, DisclosureSchedule.end_date)
            .where(
                DisclosureSchedule.actual_date >= start_date,
                DisclosureSchedule.actual_date <= end_date,
                DisclosureSchedule.end_date.is_not(None),
                *symbol_predicates,
            )
            .distinct()
            .order_by(DisclosureSchedule.ts_code, DisclosureSchedule.end_date)
        )
        with self._session_factory() as session:
            rows = session.execute(statement).all()
        return tuple(DisclosureTarget(symbol, period) for symbol, period in rows)

    def corporate_symbols(
        self,
        start_date: date,
        end_date: date,
        allowed_symbols: tuple[str, ...] | None = None,
    ) -> tuple[str, ...]:
        if allowed_symbols == ():
            return ()
        repurchase_symbol_predicates = (
            (Repurchase.ts_code.in_(allowed_symbols),)
            if allowed_symbols is not None
            else ()
        )
        unlock_symbol_predicates = (
            (ShareUnlock.ts_code.in_(allowed_symbols),)
            if allowed_symbols is not None
            else ()
        )
        shareholder_symbol_predicates = (
            (ShareholderCount.ts_code.in_(allowed_symbols),)
            if allowed_symbols is not None
            else ()
        )
        symbols = union(
            select(Repurchase.ts_code.label("symbol")).where(
                Repurchase.ann_date >= start_date,
                Repurchase.ann_date <= end_date,
                *repurchase_symbol_predicates,
            ),
            select(ShareUnlock.ts_code.label("symbol")).where(
                ShareUnlock.ann_date >= start_date,
                ShareUnlock.ann_date <= end_date,
                *unlock_symbol_predicates,
            ),
            select(ShareholderCount.ts_code.label("symbol")).where(
                ShareholderCount.ann_date >= start_date,
                ShareholderCount.ann_date <= end_date,
                *shareholder_symbol_predicates,
            ),
        ).subquery()
        statement = select(symbols.c.symbol).order_by(symbols.c.symbol)
        with self._session_factory() as session:
            return tuple(session.scalars(statement))
