import json
from pathlib import Path

import pytest

from app.shared.errors import DataServiceError
from backend.tests.support.fixtures import FixtureStore
from app.shared.validation import normalize_stock_symbol


REQUIRED_KEYS = {
    "market-overview": {"marketSnapshot", "marketStatuses", "globalIndices"},
    "market-explorer": {"commodityCategories", "heatmapData", "marketNews"},
    "macro-transmission": {"transmission", "analysis"},
    "macro-insights": {"compassPoints", "sandboxIndicators", "statusCards"},
    "screener": {"bootstrap", "stocks"},
    "stock-workbench": {"688525.SH", "688796.SH"},
    "portfolio": {"assets", "defaultState", "prices", "storageKey"},
}


def test_fixture_store_reads_envelope(tmp_path: Path) -> None:
    (tmp_path / "sample.json").write_text(
        json.dumps(
            {
                "data": {"value": 7},
                "meta": {
                    "source": "fixture",
                    "asOf": "2026-08-15T00:00:00+00:00",
                    "version": "sample-v1",
                    "stale": False,
                },
            }
        ),
        encoding="utf-8",
    )
    snapshot = FixtureStore(tmp_path).read("sample")
    assert snapshot.raw == {"value": 7}
    assert snapshot.meta.version == "sample-v1"


def test_fixture_store_rejects_invalid_source_payload(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("[]", encoding="utf-8")
    with pytest.raises(DataServiceError) as raised:
        FixtureStore(tmp_path).read("broken")
    assert raised.value.code == "INVALID_SOURCE_PAYLOAD"


@pytest.mark.parametrize("value", ["688525.SH", " 688525.sh "])
def test_normalize_stock_symbol(value: str) -> None:
    assert normalize_stock_symbol(value) == "688525.SH"


def test_normalize_stock_symbol_rejects_invalid_value() -> None:
    with pytest.raises(DataServiceError) as raised:
        normalize_stock_symbol("not-a-symbol")
    assert raised.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("name,required_keys", REQUIRED_KEYS.items())
def test_generated_fixture_contains_required_data_keys(
    name: str,
    required_keys: set[str],
) -> None:
    snapshot = FixtureStore.default().read(name)
    assert required_keys <= set(snapshot.raw)
