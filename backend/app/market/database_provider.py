from __future__ import annotations

from app.shared.repository import SnapshotRepository


class MarketDatabaseProvider:
    def __init__(self, repository: SnapshotRepository) -> None:
        self._repository = repository

    def read_overview(self):
        return self._repository.read("market-overview")

    def read_explorer(self):
        return self._repository.read("market-explorer")
