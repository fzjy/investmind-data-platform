from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import PublishedSnapshot


DAILY_PRODUCTS = frozenset(
    {"market-overview", "market-explorer", "screener", "portfolio"}
)
STALE_REASON = "SNAPSHOT_BEHIND_EXPECTED_DATE"


@dataclass(frozen=True, slots=True)
class FreshnessReport:
    checked_at: datetime
    expected_business_date: date
    stale_products: tuple[str, ...]
    missing_products: tuple[str, ...]
    missing_business_dates: tuple[date, ...]

    @property
    def healthy(self) -> bool:
        return not (
            self.stale_products
            or self.missing_products
            or self.missing_business_dates
        )


class FreshnessService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        timezone: str = "Asia/Shanghai",
    ) -> None:
        self._session_factory = session_factory
        self._timezone = ZoneInfo(timezone)

    def inspect(
        self,
        *,
        expected_business_date: date,
        missing_business_dates: Sequence[date],
        checked_at: datetime,
    ) -> FreshnessReport:
        with self._session_factory() as session:
            pointers = self._daily_pointers(session)
            return self._report(
                pointers,
                expected_business_date=expected_business_date,
                missing_business_dates=missing_business_dates,
                checked_at=checked_at,
            )

    def refresh(
        self,
        *,
        expected_business_date: date,
        missing_business_dates: Sequence[date],
        checked_at: datetime,
    ) -> FreshnessReport:
        with self._session_factory.begin() as session:
            pointers = self._daily_pointers(session, for_update=True)
            report = self._report(
                pointers,
                expected_business_date=expected_business_date,
                missing_business_dates=missing_business_dates,
                checked_at=checked_at,
            )
            stale_products = frozenset(report.stale_products)
            for pointer in pointers:
                pointer.stale = pointer.product in stale_products
                pointer.stale_reason = STALE_REASON if pointer.stale else None
                pointer.freshness_checked_at = checked_at
            session.flush()
            return report

    @staticmethod
    def _daily_pointers(
        session: Session,
        *,
        for_update: bool = False,
    ) -> tuple[PublishedSnapshot, ...]:
        statement = select(PublishedSnapshot).where(
            PublishedSnapshot.product.in_(DAILY_PRODUCTS)
            | PublishedSnapshot.product.startswith("stock-workbench:")
        )
        if for_update:
            statement = statement.with_for_update()
        return tuple(session.scalars(statement).all())

    def _report(
        self,
        pointers: Sequence[PublishedSnapshot],
        *,
        expected_business_date: date,
        missing_business_dates: Sequence[date],
        checked_at: datetime,
    ) -> FreshnessReport:
        missing_dates = tuple(sorted(missing_business_dates))
        stale_products = tuple(
            sorted(
                pointer.product
                for pointer in pointers
                if self._business_date(pointer.as_of) < expected_business_date
            )
        )
        existing_products = {pointer.product for pointer in pointers}
        missing_products = tuple(sorted(DAILY_PRODUCTS - existing_products))
        return FreshnessReport(
            checked_at=checked_at,
            expected_business_date=expected_business_date,
            stale_products=stale_products,
            missing_products=missing_products,
            missing_business_dates=missing_dates,
        )

    def _business_date(self, value: datetime) -> date:
        persisted = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return persisted.astimezone(self._timezone).date()
