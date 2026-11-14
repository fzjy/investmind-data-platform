from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.stocks.universes import (
    UniverseConfigError,
    UniverseResolver,
    UniverseSecurityState,
)


def test_representative_manifest_is_fixed_unique_and_covers_matrix() -> None:
    selection = UniverseResolver().resolve("representative-50")

    assert selection.profile == "representative-50"
    assert selection.version == "2026-09-19-v2"
    assert selection.coverage == "representative"
    assert len(selection.symbols) == 50
    assert len(set(selection.symbols)) == 50
    assert selection.symbols == tuple(sorted(selection.symbols))
    assert selection.to_payload() == {
        "profile": "representative-50",
        "version": "2026-09-19-v2",
        "size": 50,
        "coverage": "representative",
        "fingerprint": selection.fingerprint,
        "warnings": [],
    }
    assert {
        "000821.SZ",
        "600624.SH",
        "920047.BJ",
        "920185.BJ",
    } <= set(selection.symbols)
    assert not {
        "000584.SZ",
        "600804.SH",
        "430047.BJ",
        "835185.BJ",
    } & set(selection.symbols)
    assert all(
        symbol.startswith("920")
        for symbol in selection.symbols
        if symbol.endswith(".BJ")
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (["600519.SH", "600519.SH"], "duplicate"),
        (["600519.SH", "BAD"], "invalid stock symbol"),
        ([], "must not be empty"),
    ),
)
def test_custom_rejects_invalid_lists(
    tmp_path: Path,
    payload: list[str],
    message: str,
) -> None:
    path = tmp_path / "symbols.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(UniverseConfigError, match=message):
        UniverseResolver().resolve("custom", universe_file=path)


def test_custom_is_normalized_sorted_and_content_versioned(tmp_path: Path) -> None:
    path = tmp_path / "symbols.json"
    path.write_text(
        json.dumps(["600519.sh", " 000001.SZ "], ensure_ascii=False),
        encoding="utf-8",
    )

    selection = UniverseResolver().resolve("custom", universe_file=path)

    assert selection.profile == "custom"
    assert selection.coverage == "custom"
    assert selection.symbols == ("000001.SZ", "600519.SH")
    assert selection.version.startswith("custom-")
    assert len(selection.version) == len("custom-") + 12


def test_custom_requires_a_file_and_other_profiles_reject_it(tmp_path: Path) -> None:
    path = tmp_path / "symbols.json"
    path.write_text('["600519.SH"]', encoding="utf-8")

    with pytest.raises(UniverseConfigError, match="requires --universe-file"):
        UniverseResolver().resolve("custom")
    with pytest.raises(UniverseConfigError, match="only valid for custom"):
        UniverseResolver().resolve("representative-50", universe_file=path)


def test_all_uses_only_active_securities_with_deterministic_order() -> None:
    resolver = UniverseResolver(
        active_symbols=lambda: ("600519.SH", "000001.SZ", "600519.SH")
    )

    selection = resolver.resolve("all")

    assert selection.profile == "all"
    assert selection.coverage == "full-market"
    assert selection.symbols == ("000001.SZ", "600519.SH")
    assert selection.version.startswith("snapshot-")


def test_all_requires_an_active_security_source() -> None:
    with pytest.raises(UniverseConfigError, match="active security source"):
        UniverseResolver().resolve("all")


def test_security_state_changes_warn_without_replacing_fixed_symbols() -> None:
    def states(symbols: tuple[str, ...]) -> dict[str, UniverseSecurityState]:
        assert "600519.SH" in symbols
        return {
            "600519.SH": UniverseSecurityState(
                status="D",
                is_st=False,
                board="SSE Main",
            ),
            "000001.SZ": UniverseSecurityState(
                status="L",
                is_st=True,
                board="SZSE Main",
            ),
        }

    selection = UniverseResolver(security_states=states).resolve("representative-50")

    assert len(selection.symbols) == 50
    assert "600519.SH" in selection.symbols
    assert any("600519.SH" in warning and "status=D" in warning for warning in selection.warnings)
    assert any("000001.SZ" in warning and "ST" in warning for warning in selection.warnings)


def test_representative_path_is_independent_of_current_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    selection = UniverseResolver().resolve("representative-50")

    assert selection.profile == "representative-50"
    assert "600519.SH" in selection.symbols
