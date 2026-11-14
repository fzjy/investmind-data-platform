from __future__ import annotations

from app.shared.repository import SnapshotRepository
from app.shared.contracts import ProviderSnapshot
from app.shared.errors import DataServiceError


class MacroDatabaseProvider:
    def __init__(self, repository: SnapshotRepository) -> None:
        self._repository = repository

    def _read(self, product: str, version: str | None) -> ProviderSnapshot[dict[str, object]]:
        snapshot = self._repository.read(product) if version is None else self._repository.read_versioned(product, version)
        if snapshot.meta.source != "database" or (version is not None and snapshot.meta.version != version):
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "宏观产品版本或数据后端不一致")
        return snapshot

    def read_transmission(self, version: str | None = None) -> ProviderSnapshot[dict[str, object]]:
        return self._read("macro-transmission", version)

    def read_insights(self, version: str | None = None) -> ProviderSnapshot[dict[str, object]]:
        return self._read("macro-insights", version)
