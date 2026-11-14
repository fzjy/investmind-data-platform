from datetime import date

import pytest

from app.stocks.classification import contracts


def catalog_rows():
    return [
        {"index_code": "801010.SI", "industry_name": "消费", "parent_code": "0", "level": "L1", "industry_code": "110000", "src": "SW2021"},
        {"index_code": "801011.SI", "industry_name": "饮料", "parent_code": "110000", "level": "L2", "industry_code": "110100", "src": "SW2021"},
        {"index_code": "850111.SI", "industry_name": "白酒", "parent_code": "110100", "level": "L3", "industry_code": "110101", "src": "SW2021"},
    ]


def member_row(**changes):
    row = {"l1_code": "801010.SI", "l1_name": "消费", "l2_code": "801011.SI", "l2_name": "饮料",
        "l3_code": "850111.SI", "l3_name": "白酒", "ts_code": "600519.SH", "name": "样本",
        "in_date": "20260901", "out_date": None, "is_new": "Y"}
    return row | changes


def normalized(rows, flag="Y"):
    return contracts.normalize_members(rows, catalog=contracts.Catalog.from_rows(catalog_rows()),
        symbol="600519.SH", is_new=flag, window_start=date(2023, 9, 18), as_of=date(2026, 9, 18))


def test_explicit_parent_alias_and_exclusive_out_boundary():
    catalog = contracts.Catalog.from_rows(catalog_rows())
    assert catalog.nodes["850111.SI"]["parent_id"] == "801011.SI"
    result = normalized([member_row(in_date="20200101", out_date="20260918", is_new="N")], "N")[0]
    assert result.effective_from == date(2020, 1, 1)
    assert result.effective_to == date(2026, 9, 17)
    assert result.out_date == date(2026, 9, 18)
    assert result.path == ("801010.SI", "801011.SI", "850111.SI")


def test_ambiguous_parent_alias_is_rejected():
    rows = catalog_rows()
    rows.append(rows[0] | {"index_code": "801020.SI"})
    with pytest.raises(ValueError, match="ambiguous"):
        contracts.Catalog.from_rows(rows)


@pytest.mark.parametrize("change", [{"src": "SW2014"}, {"level": "L2"}, {"parent_code": "absent"}, {"industry_name": ""}])
def test_catalog_bad_source_level_parent_or_name_is_rejected(change):
    rows = catalog_rows()
    rows[0] |= change
    with pytest.raises(ValueError):
        contracts.Catalog.from_rows(rows)


@pytest.mark.parametrize("change", [
    {"ts_code": "000001.SZ"}, {"in_date": None}, {"in_date": "20260230"},
    {"out_date": "20260901"}, {"is_new": "N"}, {"l3_name": "错误名称"}, {"l2_code": "801010.SI"},
])
def test_invalid_member_is_rejected(change):
    with pytest.raises(ValueError):
        normalized([member_row(**change)])


def test_historical_missing_out_is_rejected():
    with pytest.raises(ValueError, match="terminal"):
        normalized([member_row(is_new="N")], "N")


def test_exact_duplicates_are_idempotent_but_conflicting_ends_rejected():
    row = member_row()
    assert len(normalized([row, row])) == 1
    with pytest.raises(ValueError, match="conflict"):
        normalized([row, member_row(out_date="20261001")])


def test_overlap_is_rejected_and_outside_window_not_retained():
    with pytest.raises(ValueError, match="overlap"):
        normalized([member_row(), member_row(in_date="20260902")])
    assert normalized([member_row(in_date="20220101", out_date="20230101", is_new="N")], "N") == []


def test_empty_catalog_cannot_validate_members():
    with pytest.raises(ValueError):
        contracts.Catalog.from_rows([])
