from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import PageSnapshot, PublishedSnapshot
from app.shared.contracts import DataMeta, ProviderSnapshot
from app.shared.errors import DataServiceError


class SnapshotRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def read(self, product: str) -> ProviderSnapshot[dict[str, object]]:
        with self._session_factory() as session:
            pointer = session.get(PublishedSnapshot, product)
            if pointer is None:
                raise DataServiceError("DATA_NOT_READY", f"数据产品 {product} 尚未发布")
            payload = pointer.payload
            return ProviderSnapshot(
                payload,
                DataMeta(
                    source="database",
                    asOf=pointer.as_of.isoformat(),
                    version=pointer.version,
                    stale=pointer.stale,
                    staleReason=pointer.stale_reason,
                ),
            )

    def read_versioned(self, product: str, version: str) -> ProviderSnapshot[dict[str, object]]:
        with self._session_factory() as session:
            row = session.query(PageSnapshot).filter(PageSnapshot.product == product, PageSnapshot.snapshot_version == version).one_or_none()
            if row is None:
                raise DataServiceError("DATA_NOT_FOUND", f"数据产品 {product} 的版本 {version} 不存在")
            payload = row.payload
            if product.startswith("stock-workbench:"):
                from app.stocks.normalizer import historical_workbench
                payload = historical_workbench(payload)
            return ProviderSnapshot(payload, DataMeta(source="database", asOf=row.as_of.isoformat(), version=version, stale=False))
