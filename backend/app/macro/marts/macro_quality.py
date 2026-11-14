from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from math import isfinite

from app.macro.catalog import MacroSeriesCatalog
from app.macro.import_lineage import is_verified_import
from app.macro.derivations import derive_macro_insights, same_derived_contract
from app.macro.state_rules import macro_states_valid
from app.db.models import MacroObservation
from app.marts.contracts import MartCandidate, QualityResult
from app.marts.dto import validate_product_payload


STATUS_FIELDS = frozenset({
    "id", "name", "region", "category", "frequency", "unit", "actualSource",
    "sourceUrl", "fallbackUsed", "stale", "staleReason", "observedAt",
    "releasedAt", "lastCheckedAt", "lastSuccessAt",
})


def _statuses(candidate: MartCandidate) -> list[Mapping[str, object]]:
    raw = candidate.payload.get("seriesStatus")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def observation_identity_unique(observations: Sequence[MacroObservation]) -> bool:
    keys = [
        (row.source, row.series_id, row.observed_at,
         row.released_at.replace(tzinfo=UTC) if row.released_at.tzinfo is None else row.released_at.astimezone(UTC),
         row.revision)
        for row in observations
    ]
    return len(keys) == len(set(keys))


def raw_observations_valid(catalog: MacroSeriesCatalog, observations: Sequence[MacroObservation]) -> bool:
    definitions = {item.id: item for item in catalog.series}
    for row in observations:
        definition = definitions.get(row.series_id)
        if definition is None or any(
            getattr(row, field) != getattr(definition, field)
            for field in ("region", "frequency", "unit")
        ):
            return False
        if row.source not in {source.provider for source in definition.sources} and not is_verified_import(row):
            return False
        if not isinstance(row.value, Decimal) or not row.value.is_finite():
            return False
        if not isinstance(row.observed_at, date) or not isinstance(row.released_at, datetime):
            return False
        if row.observed_at > row.released_at.date():
            return False
    return True


def _finite_point(point: Mapping[str, object]) -> bool:
    value = point.get("value")
    return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)


def _derived_valid(catalog: MacroSeriesCatalog, candidates: Sequence[MartCandidate],
                   observations: Sequence[MacroObservation] | None) -> bool:
    try:
        insights = next(c.payload for c in candidates if c.product == 'macro-insights')
        transmission = next(c.payload for c in candidates if c.product == 'macro-transmission')
        original = {item['id']: item['series'][0]['values'] for item in insights['trendDefinitions']}
        if observations is not None:
            from app.macro.marts.macro import select_catalog_observations
            cutoff = next(c.as_of for c in candidates if c.product == 'macro-insights')
            selected = select_catalog_observations(observations, catalog, cutoff)
            bound = {item.id: [] for item in catalog.series}
            for row in sorted(selected, key=lambda row: (row.series_id, row.observed_at)):
                bound[row.series_id].append({'month': row.observed_at.isoformat(), 'value': float(row.value)})
            if original != bound:
                return False
        expected = derive_macro_insights(original, insights['seriesStatus'])
        if not macro_states_valid(insights):
            return False
        if not same_derived_contract(insights.get('derivedInsights'), expected) or not same_derived_contract(insights.get('compassPoints'), expected['compass']['points']):
            return False
        # Four historical cards must carry the same intersection, values and units.
        card_list = transmission['analysis']['cards']
        ids = [item['id'] for item in card_list]
        allowed = {'cn.ppi_cpi_spread', 'cn.m2_m1_spread', 'cn.govbond_dr007_spread', 'us.treasury_fed_funds_spread'}
        expected_ids = {item['id'] for item in expected['series'] if item['id'] in allowed and item['points']}
        if len(ids) != len(set(ids)) or set(ids) != expected_ids:
            return False
        cards = {item['id']: item for item in card_list}
        raw = {key: {p['month']: p['value'] for p in points} for key, points in original.items()}
        for result in expected['series']:
            if result['id'] not in allowed:
                continue
            card = cards.get(result['id'])
            if not result['points']:
                if card is not None:
                    return False
                continue
            first, second = result['inputIds']
            meta = {'first': first, 'second': second, 'resultUnit': result['unit'], 'frequency': result['frequency'],
                    'latestPeriod': result['latestPeriod'], 'status': result['status'], 'reason': result['reason'],
                    'primaryLabel': catalog.require(first).name, 'secondaryLabel': catalog.require(second).name,
                    'resultLabel': result['title']}
            if (card is None or card.get('title') != result['title']
                    or not same_derived_contract({key: card['meta'].get(key) for key in meta}, meta)):
                return False
            wanted = [(p['month'], raw[first][p['month']], raw[second][p['month']], p['value'], p['value']) for p in result['points']]
            actual = [(p['quarter'], p['primary'], p['secondary'], p['result'], p['value']) for p in card['points']]
            if not same_derived_contract(actual, wanted):
                return False
        return True
    except (KeyError, TypeError, ValueError, StopIteration, AttributeError):
        return False


def _values_valid(catalog: MacroSeriesCatalog, candidates: Sequence[MartCandidate], observations: Sequence[MacroObservation]) -> bool:
    definitions = {item.id: item for item in catalog.series}
    offline = {(row.series_id, row.source) for row in observations if is_verified_import(row)}
    for candidate in candidates:
        for item in _statuses(candidate):
            definition = definitions.get(item.get("id"))
            if definition is None:
                return False
            if any(item.get(field) != getattr(definition, field) for field in ("region", "category", "frequency", "unit")):
                return False
            actual_source = item.get("actualSource")
            if (actual_source is not None
                    and actual_source not in {source.provider for source in definition.sources}
                    and (definition.id, actual_source) not in offline):
                return False
            observed, released = item.get("observedAt"), item.get("releasedAt")
            if (observed is None) != (released is None):
                return False
            if observed is not None:
                if not isinstance(observed, str) or not isinstance(released, str):
                    return False
                try:
                    observed_date = date.fromisoformat(observed)
                    released_date = datetime.fromisoformat(released)
                except ValueError:
                    return False
                if (released_date.tzinfo is None or observed_date > released_date.date()
                        or released_date.astimezone(UTC) > candidate.as_of.astimezone(UTC)):
                    return False
    for candidate in candidates:
        if candidate.product == "macro-insights":
            for indicator in candidate.payload.get("sandboxIndicators", []):
                definition = definitions.get(indicator.get("id"))
                if definition is None or indicator.get("unit") != definition.unit or indicator.get("groupId") != definition.category:
                    return False
                if any(not _finite_point(point) for point in indicator.get("values", [])):
                    return False
            for trend in candidate.payload.get("trendDefinitions", []):
                for series in trend.get("series", []):
                    if any(not _finite_point(point) for point in series.get("values", [])):
                        return False
        if candidate.product == "macro-transmission":
            for mode in candidate.payload.get("transmission", {}).get("modes", []):
                for node in mode.get("mainNodes", []):
                    if any(not _finite_point(point) for point in node.get("series", [])):
                        return False
            for card in candidate.payload.get("analysis", {}).get("cards", []):
                if any(not _finite_point(point) for point in card.get("points", [])):
                    return False
    return True


def evaluate_macro_quality(
    catalog: MacroSeriesCatalog,
    candidates: Sequence[MartCandidate],
    statuses: Sequence[object],
    *, observations: Sequence[MacroObservation] | None = None,
    enterprise_analysis: dict | None = None,
    enterprise_required: bool = False,
    extension_expected: dict | None = None,
    extension_required: bool = False,
) -> tuple[QualityResult, ...]:
    ids = tuple(item.id for item in catalog.series)
    catalog_ids = set(ids)
    by_product = {candidate.product: candidate for candidate in candidates}
    expected_products = {"macro-transmission", "macro-insights"}
    pair_complete = (
        len(candidates) == 2 and set(by_product) == expected_products
        and len({candidate.as_of for candidate in candidates}) == 1
        and candidates[0].payload.get("seriesStatus") == candidates[1].payload.get("seriesStatus")
    )
    payload_lists = [_statuses(candidate) for candidate in candidates]
    structure_valid = pair_complete and all(
        not validate_product_payload(candidate.product, candidate.payload)
        and isinstance(candidate.payload.get("seriesStatus"), list)
        and len(_statuses(candidate)) == len(ids)
        and all(STATUS_FIELDS <= item.keys() for item in _statuses(candidate))
        for candidate in candidates
    )
    identity_unique = all(
        [item.get("id") for item in rows] == list(ids)
        for rows in payload_lists
    ) and (not statuses or [getattr(item, "series_id", None) for item in statuses] == list(ids))
    insight_rows = _statuses(by_product["macro-insights"]) if "macro-insights" in by_product else []
    available = {str(item["id"]) for item in insight_rows if item.get("observedAt") is not None and item.get("id") in catalog_ids}
    regions = {item.region for item in catalog.series if item.id in available}
    categories = {item.category for item in catalog.series if item.id in available}
    missing = catalog_ids - available
    stale = [str(item.get("id")) for item in insight_rows if item.get("stale")]
    fallback = [str(item.get("id")) for item in insight_rows if item.get("fallbackUsed")]
    from app.macro.enterprise_contract import valid_enterprise_analysis
    from app.macro.derivations import same_derived_contract
    supplied = by_product.get('macro-transmission')
    enterprise = supplied.payload.get('enterpriseAnalysis') if supplied is not None else None
    enterprise_valid = (not enterprise_required and enterprise is None and enterprise_analysis is None) or (
        enterprise_analysis is not None and valid_enterprise_analysis(enterprise)
        and same_derived_contract(enterprise, enterprise_analysis))
    from app.macro.extensions.view import extension_view_valid
    extension_values = [c.payload.get('extensionInsights') for c in candidates]
    extension_valid = (not extension_required and extension_expected is None and all(v is None for v in extension_values)) or (
        extension_expected is not None and len(extension_values) == 2
        and all(extension_view_valid(value, extension_expected) for value in extension_values))
    return (
        QualityResult('macro-extensions-source-bound', 'blocking', extension_valid,
                      {'sourceBound': extension_expected is not None}),
        QualityResult('macro-enterprise-source-bound', 'blocking', enterprise_valid,
                      {'sourceBound': enterprise_analysis is not None}),
        QualityResult("macro-structure-valid", "blocking", structure_valid, {}),
        QualityResult("macro-series-identity-unique", "blocking", identity_unique, {}),
        QualityResult("macro-observation-identity-unique", "blocking",
                      observation_identity_unique(observations or ()), {}),
        QualityResult("macro-values-valid", "blocking",
                      structure_valid and _values_valid(catalog, candidates, observations or ())
                      and raw_observations_valid(catalog, observations or ()), {}),
        QualityResult('macro-derived-valid', 'blocking', structure_valid and _derived_valid(catalog, candidates, observations),
                      {'sourceBound': observations is not None}),
        QualityResult("macro-coverage-at-least-80", "blocking", len(available) >= 16,
                      {"available": len(available), "total": 20}),
        QualityResult("macro-regions-covered", "blocking", regions == {"CN", "US"},
                      {"regions": sorted(regions)}),
        QualityResult("macro-categories-covered", "blocking", categories == {"growth", "inflation", "money_credit", "labor", "rates_fx"},
                      {"categories": sorted(categories)}),
        QualityResult("macro-release-pair-complete", "blocking", pair_complete, {}),
        QualityResult("macro-degradation", "advisory", not (missing or stale or fallback),
                      {"missing": sorted(missing), "stale": stale, "fallback": fallback}),
    )
