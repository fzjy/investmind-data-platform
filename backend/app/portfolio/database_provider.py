from __future__ import annotations

from app.shared.repository import SnapshotRepository


class PortfolioDatabaseProvider:
    def __init__(self, repository: SnapshotRepository) -> None:
        self._repository = repository

    def read_portfolio(self):
        return self._repository.read("portfolio")
