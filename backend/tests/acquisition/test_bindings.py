import json
from pathlib import Path

from app.acquisition.catalog import load_bindings, validate_bindings

ROOT = Path(__file__).resolve().parents[3]


def test_catalog_binding_covers_every_entry():
    entries = {row['id'] for file in (ROOT/'docs/data-catalog').glob('*.json')
               for row in json.loads(file.read_text(encoding='utf-8'))}
    bindings = load_bindings(ROOT)
    assert {b.entry_id for b in bindings} == entries
    assert validate_bindings(ROOT) == []
    assert {b.item_id for b in bindings if b.entry_id == 'stock-dividend-yield'} == {
        'dividendYield', 'expectedDividendYield', 'expectedDividendYieldNTM'}
    assert all(b.subject_kind == 'security' for b in bindings if b.entry_id == 'stock-dividend')
