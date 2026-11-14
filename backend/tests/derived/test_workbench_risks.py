import pytest

from app.stocks.derived.workbench_risks import calculate_risks


@pytest.mark.parametrize("profit,comprehensive,status", [
    (100, 110, "normal"), (100, 119.99, "normal"), (100, 120, "watch"),
    (100, 60, "risk"), (-100, -60, "risk"), (0, 90, "unknown"),
])
def test_comprehensive_income_uses_absolute_gap_and_inclusive_boundaries(profit, comprehensive, status):
    inputs = {"asOf": "2026-11-10", "reports": {"income": {"2025-12-31": {
        "date": "2026-04-17", "values": {"n_income_attr_p": profit, "t_compr_income": comprehensive}}}},
        "valuations": [], "dividends": {}}
    result = calculate_risks(inputs)
    groups = result["groups"]
    assert [len(group["items"]) for group in groups] == [7, 5]
    card = next(item for item in groups[0]["items"] if item["id"] == "comprehensive_income_gap")
    assert card["status"] == status
    assert card["reportDate"] == "2025-12-31" and card["announcementDate"] == "2026-04-17"
    assert card["inputs"] and card["threshold"] and card["steps"]
    assert bool(card["missingFields"]) == (profit == 0)
    if comprehensive == 110:
        assert card["conclusion"] == "差异较小，正常"
    elif comprehensive == 119.99:
        assert "有一定差异" in card["conclusion"]
    elif comprehensive == 60:
        assert any("= -40%" in step for step in card["steps"])


def test_annual_windows_funding_and_source_units():
    from app.stocks.derived.workbench_cards import DEBT_FIELDS
    source = dict(asOf="2026-11-10", start="2016-11-10", reports={}, dividends={},
                  valuations=[["2026-11-08", "1", "3"], ["2026-11-09", "2", "2"], ["2026-11-10", "3", "1"]])
    for year in range(2016, 2026):
        period = f"{year}-12-31"
        basis = dict.fromkeys(DEBT_FIELDS, 0) | {"st_borr": 36, "total_assets": 100, "money_cap": 26,
                    "goodwill": 35, "total_share": 100, "total_hldr_eqy_inc_min_int": 100}
        for table, values in dict(balance=basis,
            income=dict(n_income_attr_p=100, t_compr_income=140, basic_eps=2, ebit=15, fin_exp_int_exp=10,
                        invest_income=50, fv_value_chg_gain=0, non_oper_income=0, non_oper_exp=0,
                        assets_impair_loss=-35, credit_impa_loss=2, revenue=100),
            cashflow=dict(n_cashflow_act=90, c_recp_borrow=10, proc_issue_bonds=0, c_recp_cap_contrib=0),
            indicator=dict(interestdebt=36)).items():
            source["reports"].setdefault(table, {})[period] = dict(date=f"{year+1}-04-10", values=values)
        source["dividends"][str(year)] = dict(basis='paid-cash-records-v1', querySource='raw.source_payload:1',
            recordedThrough=source['asOf'], covered=True, events=[dict(period=period,
            announcedAt=f"{year+1}-04-10", cashPerShare="1", status="paid", baseShare='.01',
            baseDate=f'{year}-12-31', recordDate=f'{year+1}-05-01', sourceIds=['raw.source_payload:1:row:1'])])
    result = calculate_risks(source)
    cards = {card["id"]: card for group in result["groups"] for card in group["items"]}
    assert {key: c["status"] for key, c in cards.items()} == dict(
        high_cash_high_debt="risk", goodwill_risk="watch", profit_structure_non_operating_dependency="risk",
        impairment_pressure="watch", comprehensive_income_gap="risk", eps_quality_gap="unknown",
        equity_cash_support="normal", valuation_band="watch", interest_debt_burden="risk",
        dividend_vs_financing="risk", dividend_sustainability="risk", earnings_quality_non_recurring="unknown")
    assert result["score"] == 0 and result["assessment"]["knownCount"] == 10
    assert any("270 ÷ 300" in step for step in cards["dividend_sustainability"]["steps"])


def test_seven_evaluated_cards_defer_score_and_eight_use_original_deductions():
    from app.stocks.derived.risk_assessment import assess_current
    result = calculate_risks(dict(asOf="2026-11-10", reports={}, valuations=[], dividends={}))
    cards = [item for group in result["groups"] for item in group["items"]]
    for card, status in zip(cards, ["risk", "watch"] + ["normal"] * 5):
        card["status"] = status
    assert assess_current(result)["score"] is None
    cards[7]["status"] = "normal"
    assert assess_current(result)["score"] == 56


@pytest.mark.parametrize('version', ['workbench-risk-v2', 'workbench-risk-v3'])
def test_reader_accepts_known_risk_snapshots_without_relabeling_their_policy(version):
    from copy import deepcopy
    from app.stocks.derived.risk_assessment import assess_risk_checklist
    source = calculate_risks(dict(asOf='2026-11-10', reports={}, valuations=[], dividends={}))
    source['ruleVersion'] = version
    items = [item for group in source['groups'] for item in group['items']]
    for index, item in enumerate(items):
        item['ruleVersion'] = version
        item['status'] = 'risk' if index == 0 else 'normal'
    source['assessment']['policyVersion'] = version
    original = deepcopy(source)
    result = assess_risk_checklist(source)
    assert result['ruleVersion'] == result['assessment']['policyVersion'] == version
    assert result['groups'] == original['groups']
    assert result['score'] == 82
    assert source == original


@pytest.mark.parametrize('version', ['workbench-risk-v1', 'workbench-risk-v4', 'unknown'])
def test_reader_rejects_unknown_risk_rule_versions(version):
    from app.shared.errors import DataServiceError
    from app.stocks.derived.risk_assessment import assess_risk_checklist
    source = calculate_risks(dict(asOf='2026-11-10', reports={}, valuations=[], dividends={}))
    source['ruleVersion'] = version
    with pytest.raises(DataServiceError):
        assess_risk_checklist(source)


def test_short_early_history_does_not_shorten_three_fiscal_year_window():
    source = dict(asOf="2017-05-01", valuations=[], dividends={}, reports={"income": {
        "2016-12-31": {"date": "2017-04-12", "values": {"n_income_attr_p": 30}}}})
    result = calculate_risks(source)
    card = next(c for c in result["groups"][1]["items"] if c["id"] == "dividend_sustainability")
    assert card["status"] == "unknown"
    assert any("2014" in missing for missing in card["missingFields"])
