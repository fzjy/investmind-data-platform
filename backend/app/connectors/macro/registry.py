from __future__ import annotations

from collections.abc import Iterable

from app.connectors.macro.contracts import MacroProvider


class MacroProviderRegistry:
    def __init__(self, providers: Iterable[MacroProvider]) -> None:
        self._providers: dict[str, MacroProvider] = {}
        for provider in providers:
            if provider.provider_id in self._providers:
                raise ValueError(f"duplicate macro provider id: {provider.provider_id}")
            self._providers[provider.provider_id] = provider

    def require(self, provider_id: str) -> MacroProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise LookupError(f"macro provider not registered: {provider_id}") from exc
