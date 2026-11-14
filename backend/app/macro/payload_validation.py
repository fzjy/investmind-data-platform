from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from app.shared.payload_validation import _is_number, _at, _validate_record_list


def _validate_transmission_nodes(
    value: object,
    path: str,
    errors: list[str],
) -> None:
    _validate_record_list(
        value,
        path,
        {
            "id": str,
            "title": str,
            "state": str,
            "metric": str,
            "value": str,
            "change": str,
            "series": list,
        },
        errors,
    )
    if not isinstance(value, list):
        return
    for index, node in enumerate(value):
        if isinstance(node, Mapping):
            _validate_record_list(
                node.get("series"),
                f"{path}[{index}].series",
                {"quarter": str, "value": _is_number},
                errors,
            )



def _validate_macro_extensions(payload: Mapping[str, Any], errors: list[str]) -> None:
    if 'extensionInsights' in payload:
        from app.macro.extensions.view import extension_view_valid
        if not extension_view_valid(payload['extensionInsights']):
            errors.append('extensionInsights')



def _validate_macro_transmission(
    payload: Mapping[str, Any],
    errors: list[str],
) -> None:
    _validate_macro_extensions(payload, errors)
    if 'enterpriseAnalysis' in payload:
        from app.macro.enterprise_contract import valid_enterprise_analysis
        if not valid_enterprise_analysis(payload['enterpriseAnalysis']):
            errors.append('enterpriseAnalysis')
    transmission = payload.get("transmission")
    if not isinstance(transmission, Mapping):
        return
    _validate_record_list(
        transmission.get("weather"),
        "transmission.weather",
        {"code": str, "value": str, "change": str},
        errors,
    )
    modes = transmission.get("modes")
    _validate_record_list(
        modes,
        "transmission.modes",
        {"id": str, "label": str, "targets": list, "mainNodes": list, "branches": list},
        errors,
    )
    if isinstance(modes, list):
        for mode_index, mode in enumerate(modes):
            if not isinstance(mode, Mapping):
                continue
            mode_path = f"transmission.modes[{mode_index}]"
            _validate_record_list(
                mode.get("targets"),
                f"{mode_path}.targets",
                {"label": str, "value": str},
                errors,
            )
            _validate_transmission_nodes(
                mode.get("mainNodes"),
                f"{mode_path}.mainNodes",
                errors,
            )
            branches = mode.get("branches")
            _validate_record_list(
                branches,
                f"{mode_path}.branches",
                {"id": str, "title": str, "description": str, "nodes": list},
                errors,
            )
            if isinstance(branches, list):
                for branch_index, branch in enumerate(branches):
                    if isinstance(branch, Mapping):
                        _validate_transmission_nodes(
                            branch.get("nodes"),
                            f"{mode_path}.branches[{branch_index}].nodes",
                            errors,
                        )
    _validate_record_list(
        transmission.get("industries"),
        "transmission.industries",
        {"name": str, "orderGrowth": str, "state": str},
        errors,
    )
    _validate_record_list(
        _at(payload, "analysis", "cards"),
        "analysis.cards",
        {
            "id": str,
            "title": str,
            "defaultPeriod": _is_number,
            "description": str,
            "meta": Mapping,
            "points": list,
        },
        errors,
    )



def _validate_macro_insights(
    payload: Mapping[str, Any],
    errors: list[str],
) -> None:
    _validate_macro_extensions(payload, errors)
    from app.macro.state_rules import macro_states_valid
    if not macro_states_valid(payload):
        errors.append('stateInsights')
    if 'derivedInsights' in payload:
        from app.macro.derivations import derive_macro_insights, same_derived_contract
        try:
            original = {item['id']: item['series'][0]['values'] for item in payload['trendDefinitions']}
            expected = derive_macro_insights(original, payload['seriesStatus'])
            valid = same_derived_contract(payload['derivedInsights'], expected) and same_derived_contract(payload.get('compassPoints'), expected['compass']['points'])
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            valid = False
        if not valid:
            errors.append('derivedInsights')
    _validate_record_list(
        payload.get("compassPoints"),
        "compassPoints",
        {"iva": _is_number, "month": str, "ppi": _is_number},
        errors,
    )
    _validate_record_list(
        payload.get("statusCards"),
        "statusCards",
        {"detail": str, "id": str, "label": str, "status": str, "tone": str},
        errors,
    )
    groups = payload.get("sandboxGroups")
    _validate_record_list(
        groups,
        "sandboxGroups",
        {"id": str, "indicatorIds": list, "label": str},
        errors,
    )
    indicators = payload.get("sandboxIndicators")
    _validate_record_list(
        indicators,
        "sandboxIndicators",
        {
            "axis": str,
            "color": str,
            "colorClass": str,
            "groupId": str,
            "id": str,
            "label": str,
            "unit": str,
            "values": list,
        },
        errors,
    )
    if isinstance(indicators, list):
        for index, indicator in enumerate(indicators):
            if isinstance(indicator, Mapping):
                _validate_record_list(
                    indicator.get("values"),
                    f"sandboxIndicators[{index}].values",
                    {"month": str, "value": (_is_number, type(None))},
                    errors,
                )
    presets = payload.get("sandboxPresets")
    _validate_record_list(
        presets,
        "sandboxPresets",
        {"expandedGroupId": str, "id": str, "indicatorIds": list, "label": str},
        errors,
    )
    months = payload.get("sandboxMonths")
    if not isinstance(months, list):
        errors.append("sandboxMonths")
    elif any(not isinstance(month, str) for month in months):
        errors.append("sandboxMonths")
    definitions = payload.get("trendDefinitions")
    _validate_record_list(
        definitions,
        "trendDefinitions",
        {
            "auxiliarySeriesIds": list,
            "code": str,
            "description": str,
            "formula": str,
            "id": str,
            "primarySeriesIds": list,
            "series": list,
            "title": str,
        },
        errors,
    )
    if isinstance(definitions, list):
        for definition_index, definition in enumerate(definitions):
            if not isinstance(definition, Mapping):
                continue
            series = definition.get("series")
            series_path = f"trendDefinitions[{definition_index}].series"
            _validate_record_list(
                series,
                series_path,
                {"color": str, "id": str, "label": str, "values": list},
                errors,
            )
            if isinstance(series, list):
                for series_index, item in enumerate(series):
                    if isinstance(item, Mapping):
                        _validate_record_list(
                            item.get("values"),
                            f"{series_path}[{series_index}].values",
                            {"month": str, "value": (_is_number, type(None))},
                            errors,
                        )
