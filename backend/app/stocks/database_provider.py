from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db.models import StockScreenerDaily, StockWorkbenchSnapshot
from app.shared.repository import SnapshotRepository
from app.shared.contracts import DataMeta, ProviderSnapshot
from app.shared.errors import DataServiceError


class StocksDatabaseProvider:
    def __init__(self, repository: SnapshotRepository, session_factory=None) -> None:
        self._repository = repository
        self._session_factory = session_factory

    def read_screener_universe(self):
        try:
            return self._repository.read("screener")
        except DataServiceError as error:
            if error.code != "DATA_NOT_READY" or self._session_factory is None:
                raise
            with self._session_factory() as session:
                latest = session.scalar(select(func.max(StockScreenerDaily.as_of)))
                if latest is None:
                    raise
                rows = session.scalars(select(StockScreenerDaily).where(StockScreenerDaily.as_of == latest)).all()
                if not rows:
                    raise
                version = rows[0].snapshot_version
                stocks = [dict(row.fields) for row in rows]
                return ProviderSnapshot({"bootstrap": {"totalCount": len(stocks)}, "stocks": stocks}, DataMeta(source="database", asOf=latest.isoformat(), version=version, stale=False))

    def read_workbench(self, symbol: str):
        try:
            return self._repository.read(f"stock-workbench:{symbol}")
        except DataServiceError as error:
            if error.code != "DATA_NOT_READY" or self._session_factory is None:
                raise
            with self._session_factory() as session:
                row = session.scalar(select(StockWorkbenchSnapshot).where(StockWorkbenchSnapshot.ts_code == symbol).order_by(StockWorkbenchSnapshot.as_of.desc()))
                if row is None:
                    raise
                from app.stocks.normalizer import historical_workbench
                return ProviderSnapshot(historical_workbench(row.payload), DataMeta(source="database", asOf=row.as_of.isoformat(), version=row.snapshot_version, stale=False))
