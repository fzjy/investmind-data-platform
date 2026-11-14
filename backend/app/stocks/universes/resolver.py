from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from typing import Any, cast

from app.stocks.universes.models import (
    UniverseCoverage,
    UniverseProfile,
    UniverseSecurityState,
    UniverseSelection,
)


REPRESENTATIVE_PROFILE = "representative-50"
REPRESENTATIVE_VERSION = "2026-09-19-v2"
DEFAULT_REPRESENTATIVE_PATH = (
    Path(__file__).resolve().parents[3]
    / "config"
    / "universes"
    / "representative-50.json"
)
STOCK_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
SECTOR_MINIMUMS = {
    "tech": 10,
    "consumer": 8,
    "finance": 6,
    "healthcare": 5,
    "industrial": 5,
    "energy-materials": 5,
}
REQUIRED_GROUPS = {
    "board": {"sse-main", "szse-main", "star", "chinext", "bse"},
    "size": {"large-cap", "mid-cap", "small-cap"},
    "company-action": {
        "dividend",
        "repurchase",
        "unlock",
        "shareholder-change",
        "no-recent-action",
    },
}
REQUIRED_TAG_MINIMUMS = {
    "st": 3,
    "recent-listing": 2,
    "loss-or-incomplete": 2,
}


class UniverseConfigError(ValueError):
    pass


class UniverseResolver:
    def __init__(
        self,
        *,
        representative_path: Path | None = None,
        active_symbols: Callable[[], Iterable[str]] | None = None,
        security_states: Callable[
            [tuple[str, ...]], Mapping[str, UniverseSecurityState]
        ]
        | None = None,
    ) -> None:
        self._representative_path = representative_path or DEFAULT_REPRESENTATIVE_PATH
        self._active_symbols = active_symbols
        self._security_states = security_states

    def resolve(
        self,
        profile: str,
        *,
        universe_file: Path | None = None,
    ) -> UniverseSelection:
        if profile not in {REPRESENTATIVE_PROFILE, "custom", "all"}:
            raise UniverseConfigError(f"unsupported universe profile: {profile}")
        if profile == "custom":
            if universe_file is None:
                raise UniverseConfigError("custom universe requires --universe-file")
            return self._resolve_custom(universe_file)
        if universe_file is not None:
            raise UniverseConfigError("--universe-file is only valid for custom universe")
        if profile == "all":
            return self._resolve_all()
        return self._resolve_representative()

    def _resolve_representative(self) -> UniverseSelection:
        payload = self._read_json(self._representative_path)
        entries = self._validate_representative(payload)
        symbols = tuple(sorted(entry["tsCode"] for entry in entries))
        warnings = self._state_warnings(entries, symbols)
        return self._selection(
            profile="representative-50",
            version=REPRESENTATIVE_VERSION,
            coverage="representative",
            symbols=symbols,
            warnings=warnings,
        )

    def _resolve_custom(self, path: Path) -> UniverseSelection:
        payload = self._read_json(path)
        if not isinstance(payload, list) or not all(
            isinstance(value, str) for value in payload
        ):
            raise UniverseConfigError("custom universe must be a JSON list of symbols")
        symbols = self._normalize_symbols(
            payload,
            allow_empty=False,
            reject_duplicates=True,
        )
        content_fingerprint = self._fingerprint_payload({"symbols": symbols})
        return self._selection(
            profile="custom",
            version=f"custom-{content_fingerprint[:12]}",
            coverage="custom",
            symbols=symbols,
        )

    def _resolve_all(self) -> UniverseSelection:
        if self._active_symbols is None:
            raise UniverseConfigError("all universe requires an active security source")
        symbols = self._normalize_symbols(
            self._active_symbols(),
            allow_empty=False,
            reject_duplicates=False,
        )
        snapshot_fingerprint = self._fingerprint_payload({"symbols": symbols})
        return self._selection(
            profile="all",
            version=f"snapshot-{snapshot_fingerprint[:12]}",
            coverage="full-market",
            symbols=symbols,
        )

    def _validate_representative(self, payload: object) -> tuple[dict[str, Any], ...]:
        if not isinstance(payload, dict):
            raise UniverseConfigError("representative universe must be a JSON object")
        if payload.get("schemaVersion") != 1:
            raise UniverseConfigError("representative universe schemaVersion must be 1")
        if payload.get("profile") != REPRESENTATIVE_PROFILE:
            raise UniverseConfigError("representative universe profile is invalid")
        if payload.get("version") != REPRESENTATIVE_VERSION:
            raise UniverseConfigError("representative universe version is invalid")
        if payload.get("coverage") != "representative":
            raise UniverseConfigError("representative universe coverage is invalid")
        try:
            date.fromisoformat(str(payload.get("selectedAt")))
        except ValueError as exc:
            raise UniverseConfigError("representative selectedAt must be YYYY-MM-DD") from exc

        raw_entries = payload.get("symbols")
        if not isinstance(raw_entries, list) or len(raw_entries) != 50:
            raise UniverseConfigError("representative universe must contain exactly 50 symbols")

        entries: list[dict[str, Any]] = []
        raw_symbols: list[str] = []
        tag_counts: Counter[str] = Counter()
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                raise UniverseConfigError("representative symbol entry must be an object")
            raw_symbol = raw_entry.get("tsCode")
            tags = raw_entry.get("tags")
            reason = raw_entry.get("reason")
            if not isinstance(raw_symbol, str):
                raise UniverseConfigError("representative tsCode must be a string")
            symbol = self._normalize_symbol(raw_symbol)
            if symbol != raw_symbol:
                raise UniverseConfigError(
                    f"representative symbol must already be normalized: {raw_symbol}"
                )
            if not isinstance(tags, list) or not tags or not all(
                isinstance(tag, str) and tag for tag in tags
            ):
                raise UniverseConfigError(f"representative tags are invalid: {symbol}")
            if not isinstance(reason, str) or not reason.strip():
                raise UniverseConfigError(f"representative reason is missing: {symbol}")
            raw_symbols.append(symbol)
            tag_counts.update(cast(list[str], tags))
            entries.append({"tsCode": symbol, "tags": tuple(tags), "reason": reason})

        if len(set(raw_symbols)) != len(raw_symbols):
            raise UniverseConfigError("representative universe contains duplicate symbols")
        for tag, minimum in {**SECTOR_MINIMUMS, **REQUIRED_TAG_MINIMUMS}.items():
            if tag_counts[tag] < minimum:
                raise UniverseConfigError(
                    f"representative coverage requires at least {minimum} tag={tag}"
                )
        for group, required_tags in REQUIRED_GROUPS.items():
            missing = sorted(required_tags - set(tag_counts))
            if missing:
                raise UniverseConfigError(
                    f"representative coverage group {group} is missing: {', '.join(missing)}"
                )
        return tuple(entries)

    def _state_warnings(
        self,
        entries: tuple[dict[str, Any], ...],
        symbols: tuple[str, ...],
    ) -> tuple[str, ...]:
        if self._security_states is None:
            return ()
        states = self._security_states(symbols)
        tags_by_symbol = {
            entry["tsCode"]: set(cast(tuple[str, ...], entry["tags"]))
            for entry in entries
        }
        warnings: list[str] = []
        for symbol in symbols:
            state = states.get(symbol)
            if state is None:
                continue
            if state.status != "L":
                warnings.append(f"{symbol} current status={state.status or 'unknown'}")
            expected_st = "st" in tags_by_symbol[symbol]
            if state.is_st != expected_st:
                warnings.append(
                    f"{symbol} ST state changed: manifest={expected_st} current={state.is_st}"
                )
        return tuple(warnings)

    def _selection(
        self,
        *,
        profile: UniverseProfile,
        version: str,
        coverage: UniverseCoverage,
        symbols: tuple[str, ...],
        warnings: tuple[str, ...] = (),
    ) -> UniverseSelection:
        fingerprint = self._fingerprint_payload(
            {
                "profile": profile,
                "version": version,
                "coverage": coverage,
                "symbols": symbols,
            }
        )
        return UniverseSelection(
            profile=profile,
            version=version,
            coverage=coverage,
            symbols=symbols,
            fingerprint=fingerprint,
            warnings=warnings,
        )

    def _normalize_symbols(
        self,
        values: Iterable[str],
        *,
        allow_empty: bool,
        reject_duplicates: bool,
    ) -> tuple[str, ...]:
        raw_values = tuple(values)
        if not raw_values and not allow_empty:
            raise UniverseConfigError("universe must not be empty")
        normalized = tuple(self._normalize_symbol(value) for value in raw_values)
        if reject_duplicates and len(set(normalized)) != len(normalized):
            raise UniverseConfigError("universe contains duplicate symbols")
        return tuple(sorted(set(normalized)))

    @staticmethod
    def _normalize_symbol(value: str) -> str:
        symbol = value.strip().upper()
        if not STOCK_SYMBOL_PATTERN.fullmatch(symbol):
            raise UniverseConfigError(f"invalid stock symbol: {value}")
        return symbol

    @staticmethod
    def _read_json(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise UniverseConfigError(f"cannot read universe file: {path}") from exc
        except json.JSONDecodeError as exc:
            raise UniverseConfigError(f"universe file is not valid JSON: {path}") from exc

    @staticmethod
    def _fingerprint_payload(payload: object) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
