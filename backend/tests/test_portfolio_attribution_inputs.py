from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.stocks.derived.attribution import earnings_timeline
from app.portfolio.attribution_inputs import load_attribution_inputs, valid_attribution_inputs


def row(period, announced, profit, *, revised=None):
    return SimpleNamespace(ts_code="A", end_date=date.fromisoformat(period),
        ann_date=date.fromisoformat(announced), f_ann_date=date.fromisoformat(revised) if revised else None,
        report_type="1", update_flag="0", source_id=f"test:{period}:{profit}",
        data={"n_income_attr_p": profit})


def test_ttm_uses_visible_versions_and_does_not_leak_later_corrections():
    rows = [row("2022-06-30", "2022-08-20", 40), row("2022-12-31", "2023-03-20", 100),
            row("2023-06-30", "2023-08-20", 60),
            row("2023-06-30", "2023-08-20", 70, revised="2024-04-20")]
    result = earnings_timeline(rows, date(2024, 5, 1))
    assert result[-2]["profitTtm"] == 120
    assert result[-2]["knownAt"] == "2023-08-20"
    assert result[-1]["profitTtm"] == 130
    assert result[-1]["knownAt"] == "2024-04-20"
    assert len(earnings_timeline(rows, date(2023, 10, 23))) == len(result) - 1


def test_missing_ttm_leg_and_conflicting_same_version_do_not_fall_back():
    incomplete = [row("2022-12-31", "2023-03-20", 100), row("2023-06-30", "2023-08-20", 60)]
    assert earnings_timeline(incomplete, date(2023, 10, 23))[-1]["profitTtm"] is None
    conflict = incomplete + [row("2023-06-30", "2023-08-20", 61), row("2022-06-30", "2022-08-20", 40)]
    assert earnings_timeline(conflict, date(2023, 10, 23))[-1]["profitTtm"] is None


def test_zero_and_negative_ttm_are_preserved_for_the_consumer():
    result = earnings_timeline([row("2022-12-31", "2023-03-20", Decimal("-10"))], date(2023, 10, 23))
    assert result[0]["profitTtm"] == -10


def test_publish_inputs_read_actual_core_and_keep_one_earlier_anchor(pg_factory):
    from app.db.models import DailyBasic, IncomeStatement, Security


    factory = pg_factory
    with factory.begin() as session:
        session.add(Security(ts_code="A", symbol="A", name="甲"))
        session.flush()
        for item in [row("2022-06-30", "2022-08-20", 40),
                     row("2022-12-31", "2023-03-20", 100),
                     row("2023-06-30", "2023-08-20", 60)]:
            session.add(IncomeStatement(ts_code=item.ts_code, end_date=item.end_date, ann_date=item.ann_date,
                report_type=item.report_type, update_flag=item.update_flag, data=item.data))
        session.add(DailyBasic(ts_code="A", trade_date=date(2023, 10, 23), data={"total_share": 10}))
    result = load_attribution_inputs(factory, {"A": "2023-10-23"}, date(2023, 10, 23))
    assert result["assets"]["A"]["earnings"][0]["profitTtm"] == 120
    assert len(result["assets"]["A"]["earnings"]) == 1
    assert result["assets"]["A"]["shares"][0]["value"] == 100000
    assert valid_attribution_inputs(result, ("A",), "2023-10-23")


def test_bad_attribution_payload_fails_validation_without_raising():
    from copy import deepcopy

    good = dict(asOf="2023-10-23", method="ttm-profit-per-current-share",
                assets={"A": dict(earnings=[], shares=[])})
    for malformed in [{"date": "2023-10-23", "value": True, "sourceId": "x"},
                      {"date": "2023-10-23", "sourceId": "x"},
                      {"date": "2023-10-23", "value": 1, "sourceId": 12}]:
        value = deepcopy(good)
        value["assets"]["A"]["shares"] = [malformed]
        assert not valid_attribution_inputs(value, ("A",), "2023-10-23")


def test_unrelated_old_revision_does_not_change_current_report_disclosure():
    rows = [row("2024-06-30", "2024-08-20", 40), row("2024-12-31", "2025-03-20", 100),
            row("2025-06-30", "2025-08-20", 60)]
    baseline = earnings_timeline(rows, date(2025, 10, 1))
    unrelated = row("2020-12-31", "2021-03-20", 10, revised="2025-09-20")
    assert earnings_timeline([*rows, unrelated], date(2025, 10, 1))[-1] == baseline[-1]
    # 修订实际参与TTM的旧腿时，区分本期报告披露日与TTM版本可见日。
    related = row("2024-12-31", "2025-03-20", 105, revised="2025-09-20")
    last = earnings_timeline([*rows, related], date(2025, 10, 1))[-1]
    assert last["profitTtm"] == 125
    assert last["knownAt"] == "2025-09-20"
    assert last["reportDisclosedAt"] == "2025-08-20"
