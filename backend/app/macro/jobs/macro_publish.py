from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.db.models import MacroObservation
from app.macro.jobs.macro_series_state import MacroSeriesStateView
from app.macro.jobs.macro_sync import MacroPublishReport, MacroSeriesOutcome
from app.macro.catalog import MacroSeriesCatalog
from app.marts.contracts import QualityResult, release_version
from app.macro.marts.macro import MacroMartBuilder, select_catalog_observations
from app.macro.marts.macro_quality import evaluate_macro_quality, observation_identity_unique, raw_observations_valid
from app.marts.publication import SnapshotPublisher


class MacroQualityHistory(Protocol):
    def record_quality(self, run_id: str, results: Sequence[QualityResult]) -> None: ...


class MacroPublishJob:
    def __init__(
        self, builder: MacroMartBuilder, publisher: SnapshotPublisher,
        history: MacroQualityHistory,
    ) -> None:
        self._builder = builder
        self._publisher = publisher
        self._history = history

    def publish(
        self, run_id: str, as_of: datetime, catalog: MacroSeriesCatalog,
        statuses: tuple[MacroSeriesOutcome, ...],
        *, observations: tuple[MacroObservation, ...] | None = None,
        state_views: tuple[MacroSeriesStateView, ...] = (),
        reuse_unchanged: bool = False,
    ) -> MacroPublishReport:
        if observations is None:
            observations = self._builder.load_observations(catalog, as_of)
        observations = select_catalog_observations(observations, catalog, as_of)
        preflight = (
            QualityResult("macro-observation-identity-unique", "blocking", observation_identity_unique(observations), {}),
            QualityResult("macro-values-valid", "blocking", raw_observations_valid(catalog, observations), {}),
        )
        if any(not item.passed for item in preflight):
            quality = preflight
            self._history.record_quality(run_id, quality)
            return MacroPublishReport("blocked", None, (), quality, ())
        states = (*state_views, *statuses)
        enterprise = self._builder.load_enterprise_analysis(as_of)
        extra = {'enterprise_analysis': enterprise} if enterprise is not None else {}
        extensions = self._builder.load_extension_insights(as_of, observations)
        if extensions is not None:
            extra['extension_insights'] = extensions
        draft = self._builder.build(as_of, "pending", catalog=catalog, states=states, observations=observations, **extra)
        version = release_version(as_of, draft)
        candidates = self._builder.build(as_of, version, catalog=catalog, states=states, observations=observations, **extra)
        if release_version(as_of, candidates) != version:
            raise ValueError("macro payload must not depend on release version")
        quality = evaluate_macro_quality(catalog, candidates, statuses, observations=observations,
            enterprise_analysis=enterprise, enterprise_required=self._builder.enterprise_required,
            extension_expected=extensions, extension_required=self._builder.extension_required)
        self._history.record_quality(run_id, quality)
        if any(item.severity == "blocking" and not item.passed for item in quality):
            return MacroPublishReport("blocked", None, (), quality, ())
        if reuse_unchanged:
            current = {p.product: p for p in self._publisher.current_macro_pair()}
            if (set(current) == {c.product for c in candidates}
                and len({p.version for p in current.values()}) == 1
                and all(current[c.product].payload == c.payload
                    and current[c.product].stale == c.stale
                    and current[c.product].stale_reason == c.stale_reason for c in candidates)):
                return MacroPublishReport('completed', next(iter(current.values())).version,
                    tuple(c.product for c in candidates), quality, ())
        outcome = self._publisher.publish_many(candidates, version, quality)
        return MacroPublishReport(
            "completed", version,
            tuple(candidate.product for candidate in candidates),
            quality, outcome.promoted_products,
        )
