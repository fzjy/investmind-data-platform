from __future__ import annotations

from typing import Protocol

from app.shared.contracts import ProviderSnapshot


class PortfolioProvider(Protocol):
    def read_portfolio(self) -> ProviderSnapshot[dict[str, object]]: ...
