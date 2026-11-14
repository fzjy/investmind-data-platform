from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.macro.catalog import (
    DEFAULT_MACRO_CATALOG_FILE,
    MacroCatalogError,
    MacroSeriesCatalog,
    catalog_hashes_match,
)


EXPECTED_IDS = (
    "cn.gdp.real_yoy",
    "cn.cpi.yoy",
    "cn.cpi.core_yoy",
    "cn.ppi.yoy",
    "cn.pmi.manufacturing",
    "cn.industry.value_added_yoy",
    "cn.money.m1_yoy",
    "cn.money.m2_yoy",
    "cn.credit.tsf_stock_yoy",
    "cn.rate.dr007",
    "cn.rate.govbond_10y",
    "cn.fx.usdcny",
    "us.gdp.real_qoq_annualized",
    "us.cpi.yoy",
    "us.cpi.core_yoy",
    "us.pce.core_yoy",
    "us.labor.unemployment_rate",
    "us.labor.nonfarm_payroll_change",
    "us.rate.effective_fed_funds",
    "us.rate.treasury_10y",
)

EXPECTED_SOURCE_PROVIDERS = {
    "cn.gdp.real_yoy": ("tushare",),
    "cn.cpi.yoy": ("tushare",),
    "cn.cpi.core_yoy": ("nbs",),
    "cn.ppi.yoy": ("tushare",),
    "cn.pmi.manufacturing": ("tushare",),
    "cn.industry.value_added_yoy": ("nbs",),
    "cn.money.m1_yoy": ("tushare",),
    "cn.money.m2_yoy": ("tushare",),
    "cn.credit.tsf_stock_yoy": (),
    "cn.rate.dr007": ("tushare",),
    "cn.rate.govbond_10y": ("chinamoney",),
    "cn.fx.usdcny": (),
    "us.gdp.real_qoq_annualized": ("fred",),
    "us.cpi.yoy": ("fred", "bls"),
    "us.cpi.core_yoy": ("fred", "bls"),
    "us.pce.core_yoy": ("fred",),
    "us.labor.unemployment_rate": ("fred", "bls"),
    "us.labor.nonfarm_payroll_change": ("fred", "bls"),
    "us.rate.effective_fed_funds": ("fred", "nyfed"),
    "us.rate.treasury_10y": ("fred", "us_treasury", "tushare"),
}

EXPECTED_FRED_MAPPINGS = {
    "us.gdp.real_qoq_annualized": ("A191RL1Q225SBEA", "identity"),
    "us.cpi.yoy": ("CPIAUCSL", "pc1"),
    "us.cpi.core_yoy": ("CPILFESL", "pc1"),
    "us.pce.core_yoy": ("PCEPILFE", "pc1"),
    "us.labor.unemployment_rate": ("UNRATE", "identity"),
    "us.labor.nonfarm_payroll_change": ("PAYEMS", "chg"),
    "us.rate.effective_fed_funds": ("DFF", "identity"),
    "us.rate.treasury_10y": ("DGS10", "identity"),
}

EXPECTED_DIRECT_US_MAPPINGS = {
    "us.cpi.yoy": ("bls", "CUSR0000SA0", "value", "pc1"),
    "us.cpi.core_yoy": ("bls", "CUSR0000SA0L1E", "value", "pc1"),
    "us.labor.unemployment_rate": ("bls", "LNS14000000", "value", "identity"),
    "us.labor.nonfarm_payroll_change": ("bls", "CES0000000001", "value", "chg"),
    "us.rate.effective_fed_funds": ("nyfed", "EFFR", "percent", "identity"),
    "us.rate.treasury_10y": ("us_treasury", "BC_10YEAR", "BC_10YEAR", "identity"),
}


def _default_payload() -> dict[str, object]:
    return json.loads(DEFAULT_MACRO_CATALOG_FILE.read_text(encoding="utf-8"))


def _write_catalog(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_default_catalog_contains_the_fixed_twenty_series() -> None:
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)

    assert catalog.catalog_version == "2026-10-08-v9"
    assert tuple(item.id for item in catalog.series) == EXPECTED_IDS
    assert sum(item.region == "CN" for item in catalog.series) == 12
    assert sum(item.region == "US" for item in catalog.series) == 8
    assert {item.category for item in catalog.series} == {
        "growth",
        "inflation",
        "money_credit",
        "labor",
        "rates_fx",
    }


def test_default_catalog_encodes_the_binding_source_order_and_fred_mappings() -> None:
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)

    for series_id, expected_providers in EXPECTED_SOURCE_PROVIDERS.items():
        series = catalog.require(series_id)
        assert tuple(source.provider for source in series.sources) == expected_providers
        assert tuple(source.priority for source in series.sources) == tuple(
            range(1, len(series.sources) + 1)
        )
    for series_id, (expected_series, expected_transform) in EXPECTED_FRED_MAPPINGS.items():
        source = catalog.require(series_id).sources[0]
        assert source.provider == "fred"
        assert (source.series, source.transform) == (expected_series, expected_transform)
    for series_id, expected_mapping in EXPECTED_DIRECT_US_MAPPINGS.items():
        source = catalog.require(series_id).sources[1]
        assert (source.provider, source.series, source.field, source.transform) == expected_mapping


def test_catalog_china_provider_shape_only_not_live_verification() -> None:
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    china_sources = [
        source
        for series in catalog.series
        if series.region == "CN"
        for source in series.sources
    ]

    assert {source.provider for source in china_sources} <= {"tushare", "nbs", "chinamoney"}


def test_default_catalog_does_not_substitute_overnight_shibor_for_dr007() -> None:
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)

    source = catalog.require("cn.rate.dr007").sources[0]
    assert (source.api, source.series, source.field) == ('repo_daily','DR007.IB','weight')


def test_catalog_rejects_duplicate_series_ids(tmp_path: Path) -> None:
    payload = _default_payload()
    series = payload["series"]
    assert isinstance(series, list)
    series[1]["id"] = series[0]["id"]
    path = tmp_path / "duplicate-id.json"
    _write_catalog(path, payload)

    with pytest.raises(MacroCatalogError) as exc_info:
        MacroSeriesCatalog.load(path)

    assert str(exc_info.value) == "macro catalog contains duplicate series id: cn.gdp.real_yoy"


def test_catalog_rejects_duplicate_source_priorities(tmp_path: Path) -> None:
    payload = _default_payload()
    series = payload["series"]
    assert isinstance(series, list)
    treasury = next(item for item in series if item["id"] == "us.rate.treasury_10y")
    sources = treasury["sources"]
    assert isinstance(sources, list)
    sources[1]["priority"] = 1
    path = tmp_path / "duplicate-priority.json"
    _write_catalog(path, payload)

    with pytest.raises(MacroCatalogError) as exc_info:
        MacroSeriesCatalog.load(path)

    assert str(exc_info.value) == (
        "macro series sources have duplicate priority: us.rate.treasury_10y"
    )


def test_catalog_rejects_unknown_transform_with_a_stable_message(tmp_path: Path) -> None:
    payload = _default_payload()
    series = payload["series"]
    assert isinstance(series, list)
    sources = series[0]["sources"]
    assert isinstance(sources, list)
    sources[0]["transform"] = "annualized_guess"
    path = tmp_path / "unknown-transform.json"
    _write_catalog(path, payload)

    with pytest.raises(MacroCatalogError) as exc_info:
        MacroSeriesCatalog.load(path)

    assert str(exc_info.value) == "macro catalog has unsupported transform: annualized_guess"


@pytest.mark.parametrize("key", ("apiKey", "token", "password"))
def test_catalog_rejects_secret_shaped_keys_recursively(tmp_path: Path, key: str) -> None:
    payload = _default_payload()
    series = payload["series"]
    assert isinstance(series, list)
    sources = series[0]["sources"]
    assert isinstance(sources, list)
    sources[0]["params"] = {"nested": {key: "must-not-be-accepted"}}
    path = tmp_path / f"secret-{key}.json"
    _write_catalog(path, payload)

    with pytest.raises(MacroCatalogError) as exc_info:
        MacroSeriesCatalog.load(path)

    assert str(exc_info.value) == f"macro catalog contains forbidden secret-shaped key: {key}"


@pytest.mark.parametrize("key", ("fredApiKey", "tushareToken"))
def test_catalog_rejects_compound_secret_shaped_keys(
    tmp_path: Path,
    key: str,
) -> None:
    payload = _default_payload()
    series = payload["series"]
    assert isinstance(series, list)
    sources = series[0]["sources"]
    assert isinstance(sources, list)
    sources[0]["params"] = {key: "must-not-be-accepted"}
    path = tmp_path / f"compound-secret-{key}.json"
    _write_catalog(path, payload)

    with pytest.raises(MacroCatalogError) as exc_info:
        MacroSeriesCatalog.load(path)

    assert str(exc_info.value) == f"macro catalog contains forbidden secret-shaped key: {key}"


def test_catalog_rejects_string_source_priorities_before_coercion(tmp_path: Path) -> None:
    payload = _default_payload()
    series = payload["series"]
    assert isinstance(series, list)
    treasury = next(item for item in series if item["id"] == "us.rate.treasury_10y")
    sources = treasury["sources"]
    assert isinstance(sources, list)
    for source in sources:
        source["priority"] = "1"
    path = tmp_path / "string-priorities.json"
    _write_catalog(path, payload)

    with pytest.raises(MacroCatalogError) as exc_info:
        MacroSeriesCatalog.load(path)

    assert str(exc_info.value) == "macro catalog is invalid"


def test_catalog_hash_changes_when_file_content_changes_without_a_version_change(
    tmp_path: Path,
) -> None:
    source_bytes = DEFAULT_MACRO_CATALOG_FILE.read_bytes()
    original = tmp_path / "original.json"
    changed = tmp_path / "changed.json"
    original.write_bytes(source_bytes)
    changed.write_bytes(source_bytes + b"\n")

    original_catalog = MacroSeriesCatalog.load(original)
    changed_catalog = MacroSeriesCatalog.load(changed)

    assert original_catalog.catalog_version == changed_catalog.catalog_version
    assert original_catalog.catalog_hash != changed_catalog.catalog_hash


@pytest.mark.parametrize("line_ending", ("lf", "crlf", "mixed"))
def test_catalog_identity_is_stable_across_platform_line_endings(tmp_path: Path, line_ending: str) -> None:
    source = DEFAULT_MACRO_CATALOG_FILE.read_bytes().replace(b"\r\n", b"\n")
    original = tmp_path / "lf.json"
    original.write_bytes(source)
    lines = source.splitlines(keepends=True)
    variant = tmp_path / "variant.json"
    variant.write_bytes(b"".join(
        line.replace(b"\n", b"\r\n") if line_ending == "crlf" or (line_ending == "mixed" and index % 2) else line
        for index, line in enumerate(lines)
    ))
    assert MacroSeriesCatalog.load(variant).catalog_hash == MacroSeriesCatalog.load(original).catalog_hash


def test_verified_v7_legacy_identity_has_reproducible_original_bytes() -> None:
    # 旧 worktree 的 9 行 LF，其他行 CRLF；无需依赖那份机器专属目录。
    source = DEFAULT_MACRO_CATALOG_FILE.with_name('macro-series-v7.json').read_bytes().replace(b"\r\n", b"\n")
    lf_lines = {3, 49, 58, 59, 60, 91, 100, 101, 102}
    mixed = b"".join(line if index in lf_lines else line.replace(b"\n", b"\r\n")
                     for index, line in enumerate(source.splitlines(keepends=True), start=1))
    assert sha256(source).hexdigest() == "0840df6a99c8b7f197557dce1a68fe3afe5ae1f30c042541dcd24949b3147e9a"
    assert sha256(mixed).hexdigest() == "7c4787cfb46c09b4491c600d28ae27696259c6604e9741626b1367d3500f3fde"
    assert sha256(source.replace(b"\n", b"\r\n")).hexdigest() == "85c55cd9da189b751cafd3736d47095e920c0f8c6a7bac9db3ffa177c4562a31"
    assert catalog_hashes_match(sha256(mixed).hexdigest(), sha256(source).hexdigest())
    assert not catalog_hashes_match(sha256(mixed).hexdigest(), sha256(source + b"\n").hexdigest())
