from datetime import date
import pytest

AS_OF = date(2026, 10, 21)


def row(**changes):
    return dict(ts_code='600519.SH', end_date='20251231', ann_date='20260401',
        div_proc='实施', cash_div_tax=1, stk_div=0, stk_bo_rate=0, stk_co_rate=0,
        imp_ann_date='20260501', record_date='20260510', ex_date='20260511',
        pay_date='20260511', base_date='20260510', base_share=10, sourceId='r1') | changes


def event(**changes):
    return dict(periodEnd='2025-12-31', implementationDate='2026-05-01',
        recordDate='2026-05-10', exDate='2026-05-11', payDate='2026-05-11',
        cashStatus='paid', capitalStatus='reported-total', stockRatio=0,
        cashPerShare=1, sourceIds=['r1']) | changes


def audit(rows=None, events=None, **kw):
    from app.stocks.fiscal_contracts import audit_fiscal_disclosures
    return audit_fiscal_disclosures(rows if rows is not None else [row()],
        events if events is not None else [event()], AS_OF, **kw)


def test_terminal_query_qualifies_disclosed_scope_but_never_complete_economic_history():
    result = audit(query_complete=True)['2025']
    assert result['disclosedQualified'] is True
    assert result['queryComplete'] is True
    assert result['historyComplete'] is False
    assert audit()['2025']['disclosedQualified'] is False


@pytest.mark.parametrize('changes,reason', [
    ({'base_share':None}, 'event_capital_basis_missing'),
    ({'base_date':None}, 'event_capital_basis_missing'),
    ({'base_date':'20260512'}, 'event_capital_basis_invalid'),
    ({'imp_ann_date':None}, 'implementation_missing'),
])
def test_amount_evidence_requires_event_specific_capital_date_and_implementation(changes,reason):
    result = audit([row(**changes)],query_complete=True)['2025']
    assert result['disclosedQualified'] is False
    assert reason in result['reasons']


def test_pending_cash_missing_implementation_and_unknown_year_issue_block():
    assert not audit(events=[event(cashStatus='pending')],query_complete=True)['2025']['disclosedQualified']
    assert not audit([row(end_date=None)],query_complete=True)['2025']['disclosedQualified']
    # 未定位取消不能因已有一条看似正常的实施记录而消失。
    assert not audit([row(),row(div_proc='取消',record_date=None,ex_date=None,sourceId='cancel')],query_complete=True)['2025']['disclosedQualified']


def test_proposal_and_future_revision_do_not_poison_disclosed_year():
    rows=[row(),row(div_proc='预案',ann_date=None,sourceId='proposal'),
        row(ann_date='20261101',base_share=None,sourceId='future')]
    assert audit(rows,query_complete=True)['2025']['disclosedQualified']


def test_no_events_is_missing_not_zero_and_explicit_zero_does_not_need_capital_amount():
    assert not audit([row(div_proc='预案')],[],query_complete=True)['2025']['disclosedQualified']
    assert audit([row(cash_div_tax=0,base_share=None,base_date=None)],
        [event(cashPerShare=0,cashStatus='not_required')],query_complete=True)['2025']['disclosedQualified']


def test_unqualified_source_pointer_and_cash_mismatch_never_pass_audit():
    assert not audit(events=[event(sourceIds=['unknown'])],query_complete=True)['2025']['disclosedQualified']
    assert not audit(events=[event(cashPerShare=2)],query_complete=True)['2025']['disclosedQualified']


@pytest.mark.parametrize('changes', [{'base_date':'bad'}, {'imp_ann_date':'bad'}, {'cash_div_tax':False}])
def test_malformed_source_does_not_crash_or_qualify(changes):
    assert not audit([row(**changes)],query_complete=True)['2025']['disclosedQualified']


def test_same_cash_cannot_link_event_to_wrong_fiscal_source():
    assert not audit([row(end_date='20241231')],query_complete=True)['2025']['disclosedQualified']


def test_current_fiscal_year_stays_open():
    assert not audit([row(end_date='20260630')],[event(periodEnd='2026-06-30')],query_complete=True)['2026']['disclosedQualified']


def test_unknown_stage_blocks_but_explicit_general_meeting_proposal_does_not():
    assert not audit([row(),row(div_proc='未知阶段',sourceId='unknown')],query_complete=True)['2025']['disclosedQualified']
    assert audit([row(),row(div_proc='股东大会通过',sourceId='proposal')],query_complete=True)['2025']['disclosedQualified']


def test_capital_conflict_from_shared_ledger_blocks_disclosed_ratio():
    result=audit([row(stk_div=.1,stk_bo_rate=.2)],
        [event(capitalStatus='conflict',stockRatio=None)],query_complete=True)['2025']
    assert not result['disclosedQualified']
    assert 'capital_conflict' in result['reasons']


def test_missing_cash_year_remains_visible_with_reason_and_unknown_amount_not_zero():
    from app.stocks.shareholder_supply import annual_returns
    result=annual_returns([],{},AS_OF,
        fiscal_audits=audit(events=[event(cashStatus='missing')],query_complete=True))
    annual=result['annualReturns'][0]
    assert annual['year']=='2025' and 'event_not_paid' in annual['reasons']
    assert annual['cashAmount'] is None and annual['observedDps'] is None
    assert annual['observedPayoutRatio'] is None


def test_disclosed_ratio_is_separate_from_verified_fy_screener_values():
    from app.stocks.shareholder_supply import annual_returns
    result=annual_returns([event() | dict(cashAmount=100000,baseShares=100000)],
        {'2025':400000},AS_OF,price=10,fiscal_audits=audit(query_complete=True))
    annual=result['annualReturns'][0]
    assert annual['observedPayoutRatio']==25
    assert annual['disclosedQualified'] and not annual['complete']
    assert result['researchValues']['dividendYield'] is None
    assert result['researchValues']['payoutRatio'] is None


def test_future_complete_contract_requires_latest_closed_year_not_latest_available_year():
    from app.stocks.shareholder_supply import annual_returns
    events=[dict(fiscalYear=str(y),cashPerShare=1,cashAmount=10,stockRatio=0,baseShares=10) for y in (2022,2023,2024)]
    result=annual_returns(events,{str(y):100 for y in (2022,2023,2024)},AS_OF,price=10,history_complete=True)
    assert result['researchValues']['dividendYield'] is None
    assert result['researchValues']['payoutRatio'] is None


def test_future_complete_yield_does_not_require_profit_and_open_year_does_not_hide_three_fy():
    from app.stocks.shareholder_supply import annual_returns
    events=[dict(fiscalYear=str(y),cashPerShare=1,cashAmount=10,stockRatio=0,baseShares=10) for y in (2023,2024,2025,2026)]
    result=annual_returns(events,{'2023':100,'2024':200,'2025':400},AS_OF,price=10,history_complete=True)
    assert result['researchValues']['payoutRatio']==pytest.approx((10+5+2.5)/3)
    loss=annual_returns(events,{'2025':-100},AS_OF,price=10,history_complete=True)
    assert loss['researchValues']['dividendYield']==10
    assert loss['researchValues']['payoutRatio'] is None
