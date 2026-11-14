from __future__ import annotations

from app.macro.normalizer import normalize_macro_insights, normalize_macro_transmission
from app.macro.provider import MacroProvider
from app.shared.contracts import DataResult, success_result


class MacroService:
    def __init__(self, provider: MacroProvider) -> None:
        self._provider = provider

    def get_transmission(self, version: str | None = None) -> DataResult[dict[str, object]]:
        snapshot = self._provider.read_transmission() if version is None else self._provider.read_transmission(version)
        return success_result(normalize_macro_transmission(snapshot.raw), snapshot.meta)

    def get_insights(self, version: str | None = None) -> DataResult[dict[str, object]]:
        snapshot = self._provider.read_insights() if version is None else self._provider.read_insights(version)
        return success_result(normalize_macro_insights(snapshot.raw), snapshot.meta)
