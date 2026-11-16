from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.db.models import DailyBasic, DailyPrice, MetricValue, RiskResult, Security
from app.stocks.derived.inputs import CoreSnapshot, SecurityFormulaInputs
from app.stocks.marts.stocks import ScreenerMartBuilder
from app.marts.dto import validate_product_payload


def test_builder_never_emits_supplier_fields() -> None:
    result = ScreenerMartBuilder([{"symbol": "600000.SH", "pe": 10, "supplierFieldFoo": "secret"}]).build(date(2026, 8, 17), "v1")
    assert not any(key.startswith("supplierField") for key in result.rows[0])


def test_builder_bootstrap_is_derived_from_rows() -> None:
    result = ScreenerMartBuilder([{"symbol": "600000.SH", "majorIndustry": "金融"}]).build(date(2026, 8, 17), "v1")
    assert result.payload["bootstrap"]["totalCount"] == 1


def test_builder_bootstrap_matches_frontend_contract() -> None:
    result = ScreenerMartBuilder().build(_core_snapshot(), "v1")

    assert set(result.payload["bootstrap"]) >= {
        "presets",
        "exclusions",
        "boards",
        "categories",
        "categoryValues",
        "groups",
        "thresholds",
        "sortFields",
        "totalCount",
        "evidenceBadge",
    }
    assert result.payload["bootstrap"]["categoryValues"] == {
        "majorIndustry": [],
        "subIndustry": [],
        "concept": [],
        "region": [],
    }


def _core_snapshot() -> CoreSnapshot:
    security = Security(
        ts_code="600519.SH",
        symbol="600519",
        name="贵州茅台",
        exchange="SSE",
        status="L",
        list_date=date(2001, 8, 27),
    )
    return CoreSnapshot(
        as_of=date(2026, 8, 24),
        securities=(security,),
        daily_prices=(
            DailyPrice(
                ts_code="600519.SH",
                trade_date=date(2026, 8, 24),
                open=Decimal("1490"),
                high=Decimal("1510"),
                low=Decimal("1480"),
                close=Decimal("1500"),
                pct_chg=Decimal("1.2"),
                vol=Decimal("100"),
                amount=Decimal("1000"),
            ),
        ),
        daily_basics=(
            DailyBasic(
                ts_code="600519.SH",
                trade_date=date(2026, 8, 24),
                pe=Decimal("20"),
                pb=Decimal("5"),
                data={},
            ),
        ),
        adjustment_factors=(),
        security_inputs={
            "600519.SH": SecurityFormulaInputs(
                values={"profit": Decimal("100")},
                sources={},
                risk_values={},
            )
        },
        missing_data=(),
        financial_points=(),
        corporate_actions=(),
        classifications=(),
        macro_observations=(),
        content_documents=(),
    )


def test_screener_keeps_unknown_risk_nullable() -> None:
    candidate = ScreenerMartBuilder().build(_core_snapshot(), "v1")
    stock = candidate.payload["stocks"][0]

    assert stock["inventoryRisk"] is None
    assert stock["netDebt"] is None


def test_screener_matches_complete_frontend_contract() -> None:
    candidate = ScreenerMartBuilder().build(_core_snapshot(), "v1")
    stock = candidate.payload["stocks"][0]

    assert stock["change"] == 1.2
    assert stock["marketCap"] is None
    assert stock["dividendYears"] is None
    assert stock["industry"] == ""
    assert stock["listingYears"] > 0
    assert stock["majorIndustry"] == ""
    assert stock["subIndustry"] == ""
    assert stock["region"] == ""
    assert len(candidate.payload["bootstrap"]["presets"]) == 10
    assert any(p['id']=='ntm-dividend-hunter' for p in candidate.payload['bootstrap']['presets'])
    assert validate_product_payload("screener", candidate.payload) == ()


def test_screener_preserves_missing_change_as_null() -> None:
    snapshot = replace(
        _core_snapshot(),
        daily_prices=(
            DailyPrice(
                ts_code="600519.SH",
                trade_date=date(2026, 8, 24),
                close=Decimal("1500"),
                pct_chg=None,
            ),
        ),
    )

    candidate = ScreenerMartBuilder().build(snapshot, "v1")

    assert candidate.payload["stocks"][0]["change"] is None
    assert validate_product_payload("screener", candidate.payload) == ()


@pytest.mark.parametrize("total_mv,expected", [
    (Decimal("22452647.3574"), 2245.26473574),
    (Decimal("1234"), 0.1234),
    (Decimal("0"), 0),
    (None, None),
])
def test_screener_market_cap_converts_core_wan_yuan_to_yi_yuan(total_mv, expected) -> None:
    base = _core_snapshot()
    basic = base.daily_basics[0]
    basic.total_mv = total_mv
    candidate = ScreenerMartBuilder().build(base, "v1")
    stock = candidate.payload["stocks"][0]

    assert stock["marketCap"] == (None if expected is None else pytest.approx(expected))
    assert basic.total_mv == total_mv
    assert validate_product_payload("screener", candidate.payload) == ()


def test_screener_legacy_rows_keep_existing_yi_yuan_units() -> None:
    result = ScreenerMartBuilder([{"symbol": "600000.SH", "marketCap": 2245.26473574}]).build(date(2026, 8, 17), "v1")
    assert result.payload["stocks"][0]["marketCap"] == 2245.26473574


def test_screener_maps_bse_security_to_frontend_board_id() -> None:
    security = Security(
        ts_code="920001.BJ",
        symbol="920001",
        name="北交所样例",
        exchange="BSE",
        status="L",
    )
    snapshot = replace(
        _core_snapshot(),
        securities=(security,),
        daily_prices=(
            DailyPrice(
                ts_code="920001.BJ",
                trade_date=date(2026, 8, 24),
                close=Decimal("10"),
            ),
        ),
        daily_basics=(),
        security_inputs={},
    )

    stock = ScreenerMartBuilder().build(snapshot, "v1").payload["stocks"][0]

    assert stock["board"] == "bse"


@pytest.mark.parametrize("symbol,exchange,board,expected", [
    ("600519.SH", "SSE", "主板", "sh-main"),
    ("000001.SZ", "SZSE", "主板", "sz-main"),
    ("688111.SH", "SSE", "科创板", "star"),
    ("300750.SZ", "SZSE", "创业板", "chinext"),
    ("920002.BJ", "BSE", "北交所", "bse"),
    ("688111.SH", "SSE", None, ""),
    ("300750.SZ", "SZSE", None, ""),
    ("688111.SH", "SSE", "", ""),
    ("300750.SZ", "SZSE", "未识别板块", "未识别板块"),
])
def test_builder_normalizes_supplier_board_labels(symbol, exchange, board, expected) -> None:
    security = Security(ts_code=symbol, symbol=symbol[:6], name="板块样本",
                        exchange=exchange, board=board, status="L")
    snapshot = replace(_core_snapshot(), securities=(security,),
                       daily_prices=(DailyPrice(ts_code=symbol, trade_date=date(2026, 8, 24), close=Decimal("10")),),
                       daily_basics=(), security_inputs={})
    result = ScreenerMartBuilder().build(snapshot, "v1")
    assert result.payload["stocks"][0]["board"] == expected


def test_screener_validator_reports_missing_and_invalid_nested_fields() -> None:
    payload = ScreenerMartBuilder().build(_core_snapshot(), "v1").payload
    del payload["stocks"][0]["name"]
    payload["stocks"][0]["concepts"] = "食品饮料"
    payload["stocks"][0]["inventoryRisk"] = "unknown"

    errors = validate_product_payload("screener", payload)

    assert "stocks[0].name" in errors
    assert "stocks[0].concepts" in errors
    assert "stocks[0].inventoryRisk" in errors


def test_screener_prefetches_derived_rows_for_all_securities(pg_factory) -> None:
    factory = pg_factory
    engine = factory.kw["bind"]
    second = Security(
        ts_code="000001.SZ",
        symbol="000001",
        name="平安银行",
        exchange="SZSE",
        status="L",
        list_date=date(1991, 4, 3),
    )
    snapshot = replace(
        _core_snapshot(),
        securities=(_core_snapshot().securities[0], second),
        daily_prices=(
            *_core_snapshot().daily_prices,
            DailyPrice(
                ts_code="000001.SZ",
                trade_date=date(2026, 8, 24),
                close=Decimal("12"),
            ),
        ),
    )
    with factory.begin() as session:
        session.add_all(snapshot.securities)
        session.flush()
        session.add_all(
            [
                MetricValue(
                    ts_code=symbol,
                    metric_id="revenueGrowth",
                    as_of=snapshot.as_of,
                    formula_version="1.0.0",
                    value=Decimal("1"),
                    unit="percent",
                    quality="complete",
                    missing_fields=[],
                )
                for symbol in ("600519.SH", "000001.SZ")
            ]
        )
        session.add_all(
            [
                MetricValue(
                    ts_code=symbol,
                    metric_id="revenueGrowth",
                    as_of=snapshot.as_of,
                    formula_version="9.9.9",
                    value=Decimal("99"),
                    unit="percent",
                    quality="complete",
                    missing_fields=[],
                )
                for symbol in ("600519.SH", "000001.SZ")
            ]
        )
        session.add_all(
            [
                RiskResult(
                    ts_code=symbol,
                    risk_id="inventoryRisk",
                    as_of=snapshot.as_of,
                    status="normal",
                    inputs={},
                    formula="fixture",
                    conclusion="normal",
                    missing_fields=[],
                )
                for symbol in ("600519.SH", "000001.SZ")
            ]
        )

    selects: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def count_derived_selects(_conn, _cursor, statement, _parameters, _context, _many) -> None:
        # 只统计被测派生指标/风险的批量查询；Core 研究输入有独立读取合同。
        if statement.lstrip().upper().startswith("SELECT") and any(
            table in statement for table in ("metric_value", "risk_result")
        ):
            selects.append(statement)

    candidate = ScreenerMartBuilder(factory).build(snapshot, "v1")

    assert len(candidate.payload["stocks"]) == 2
    assert {row["revenueGrowth"] for row in candidate.payload["stocks"]} == {1}
    assert len(selects) == 2

@pytest.mark.parametrize('goodwill,expected', [('20','normal'), ('20.01','warn'), ('35','warn'), ('35.01','danger')])
def test_list_risk_thresholds_and_zero_cash(goodwill, expected):
    from app.stocks.marts.screener_list import list_risks
    from app.stocks.derived.workbench_cards import Card
    source = {'reports': {'balance': {'2025-12-31': {'values': {'goodwill':goodwill, 'total_assets':'100'}}}}, 'dividends':{}}
    cash = Card('workbenchNetCash', '2026-05-06', Decimal(0))
    row = {'financialTrends':{'annual':[{'period':'2025-12-31'}]}, 'researchValues':{'realMoneyIndex':.8}}
    risks = list_risks(row, source, cash)
    assert [r['status'] for r in risks] == ['unknown', expected, 'unknown', 'normal', 'normal']
    assert '0 元' in risks[-1]['detail']
    assert list_risks(row, source, None)[-1]['status'] == 'unknown'

def test_three_year_payout_needs_contiguous_paid_fiscal_years():
    from app.stocks.marts.screener_list import list_payouts
    from app.stocks.shareholder_returns import annual_distributions
    from app.stocks.derived.metric_dividends import dividend_years
    from backend.tests.test_dividend_ledger import row as dividend_row
    cutoff = date(2026, 5, 11)
    query = dict(querySource='raw.source_payload:10', sourceIds=['raw.source_payload:10'],
                 coverageStart='1990-01-01', coverageEnd='2026-11-14', issues=[])
    raw = dividend_row(base_share=4, base_date='20260501', sourceId='raw.source_payload:10:row:1')
    years = dividend_years([raw], cutoff, 10, query_coverage=query)
    annual = annual_distributions(years, {'2025':100000}, cutoff)
    latest = next(r for r in annual if r['year']=='2025')
    assert latest['cashAmount'] == 0 and latest['status']=='complete'
    paid_day = date(2026, 5, 12)
    paid = annual_distributions(dividend_years([raw], paid_day, 10, query_coverage=query), {'2025':100000}, paid_day)
    latest = next(r for r in paid if r['year']=='2025')
    assert latest['cashAmount'] == 20000 and latest['payoutRatio']==20
    rows = [latest | {'year':str(y)} for y in range(2023,2026)]
    result, gaps = {'researchValues':{}, 'listHistory':{}}, {}
    list_payouts(result, rows, 2025, gaps)
    assert result['researchValues']['payoutRatio'] == 20
    list_payouts(result, [rows[0],rows[2]], 2025, gaps)
    assert result['researchValues']['payoutRatio'] is None and '2024' in gaps['payoutRatio']
    rows[-1] = latest | {'parentProfit':-100000, 'payoutRatio':None}
    list_payouts(result, rows, 2025, gaps)
    assert result['listHistory']['dividends'][-1]['payoutRatio'] == -20
    assert result['researchValues']['payoutRatio'] is None


def test_equity_financing_zero_unknown_and_paid_cash_dividend_in_risk():
    from app.stocks.marts.screener_list import capital_returns, financing_risk
    reports = {'cashflow':{'2025-12-31':{'values':{'c_recp_cap_contrib':0, 'n_cashflow_act':12, 'c_recp_borrow':3}}}}
    values, missing = {}, {}
    paid = [{'year':'2025','status':'complete','cashAmount':15}]
    capital_returns(reports, paid, 2025, values, missing)
    assert '流入为零' in missing['capitalReturnRatio']
    assert '2023、2024 年未披露' in missing['capitalReturnRatioCum3y']
    status, detail = financing_risk({'reports':reports}, paid, '2025-12-31')
    assert status == 'danger' and '实付分红 15 元' in detail and '覆盖 0.8 倍' in detail


def test_new_list_contract_accepts_legacy_rows_rejects_bad_order_status_and_reason():
    import copy
    original = ScreenerMartBuilder().build(_core_snapshot(), 'v1').payload
    for mutate, key in ((lambda r:r['listRisks'].reverse(), 'listRisks'),
                         (lambda r:r['listRisks'][0].update(status='ok'), 'listRisks'),
                         (lambda r:r['listMissingReasons'].update(unknown=None), 'listMissingReasons')):
        payload = copy.deepcopy(original)
        mutate(payload['stocks'][0])
        assert 'stocks[0].'+key in validate_product_payload('screener', payload)
    del original['stocks'][0]['listRisks'], original['stocks'][0]['listMissingReasons']
    assert validate_product_payload('screener', original) == ()

def test_valuation_window_requires_listing_age_calendar_and_all_expected_observations(pg_factory):
    from types import SimpleNamespace
    from app.db.models import TradeCalendar
    from app.stocks.marts.screener_list import valuation_windows

    start, end = date(2026,8,24), date(2026,8,25)
    code = '600519.SH'
    item = SimpleNamespace(exchange='SSE', list_date=date(2001,8,27))
    source = {'start':start.isoformat(),'valuations':[[end.isoformat(),10,2]]}
    with pg_factory.begin() as s:
        def check():
            valuation_windows(s, {code:item}, {code:source}, end)
            return source['listWindowGap']
        assert '日历' in check()
        s.add_all([TradeCalendar(exchange='SSE',cal_date=day,is_open=True) for day in (start,end)])
        assert '缺 1 个非停牌交易日' in check()
        source['valuations'].append([start.isoformat(),12,1])
        assert check() is None
        item.list_date = end
        assert '上市未满' in check()

def test_ten_year_percentiles_keep_unconflicted_metric_and_reject_closed_day(pg_factory, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace
    from sqlalchemy import insert
    from app.db.models import TradeCalendar
    from app.stocks.derived.risk_inputs import load_risk_inputs
    from app.stocks.marts.screener_list import list_projection, valuation_windows
    from app.stocks import valuation_supply


    start, end = date(2016, 8, 25), date(2026, 8, 25)
    code = '600519.SH'
    security = SimpleNamespace(exchange='SSE', list_date=date(2001, 8, 27))
    calendar = {}
    for i in range((end-start).days+1):
        day = start + timedelta(days=i)
        calendar[day] = day.weekday() < 5
    raw = {d.isoformat():[10, 2] for d, opened in calendar.items() if opened}
    raw[end.isoformat()] = [12, 1]
    def history(_session, _codes, _as_of):
        return {code:{'start':start.isoformat(), 'end':end.isoformat(), 'rows':raw}}
    monkeypatch.setattr(valuation_supply, 'risk_valuation_history', history)
    with pg_factory.begin() as session:
        session.add(Security(ts_code=code, symbol=code[:6], name=code))
        session.flush()
        session.execute(insert(TradeCalendar), [
            {'exchange':'SSE', 'cal_date':d, 'is_open':opened} for d, opened in calendar.items()])
        session.add(DailyBasic(ts_code=code, trade_date=start, pe=11, pb=2))
        def project():
            sources = load_risk_inputs(session, (code,), end)
            valuation_windows(session, {code:security}, sources, end)
            row = {'listHistory':{}, 'financialTrends':{'annual':[]}, 'researchValues':{}}
            list_projection(row, sources[code], None, [], end)
            return row
        row = project()
        assert row['researchValues']['pePercentile10y'] is None
        assert 'PE 有 1 个交易日的估值缺失或冲突' in row['listMissingReasons']['pePercentile10y']
        assert row['researchValues']['pbPercentile10y'] == pytest.approx(100/len(raw))
        assert 'pbPercentile10y' not in row['listMissingReasons']
        closed = next(d for d, opened in calendar.items() if not opened)
        raw[closed.isoformat()] = [5, 0.5]
        row = project()
        for key in ('pePercentile10y','pbPercentile10y'):
            assert row['researchValues'][key] is None
            assert '交易日历不符' in row['listMissingReasons'][key]

def test_partial_year_publishes_known_paid_subtotal_without_complete_annual_amount():
    from app.stocks.marts.screener_list import list_payouts
    result = {'researchValues':{}, 'listHistory':{}}
    source = {'year':'2025','status':'partial','observedCashAmount':125,'cashAmount':None,'parentProfit':1000}
    list_payouts(result, [source], 2025, {})
    year = result['listHistory']['dividends'][-1]
    assert year['observedCashAmount'] == 125
    assert year['cashAmount'] is None and year['payoutRatio'] is None
    assert result['researchValues']['payoutRatio'] is None

def test_suspension_requires_individual_trading_evidence_for_valuation_window(pg_factory):
    from types import SimpleNamespace
    from app.db.models import Suspension, TradeCalendar
    from app.stocks.marts.screener_list import valuation_windows

    code = '600519.SH'
    first, last = date(2026,8,24), date(2026,8,25)
    history = {'start':first.isoformat(),'valuations':[[last.isoformat(),10,2]]}
    security = SimpleNamespace(exchange='SSE', list_date=date(2001,8,27))
    with pg_factory.begin() as session:
        session.add(Security(ts_code=code, symbol=code[:6], name=code))
        session.flush()
        def check():
            valuation_windows(session, {code:security}, {code:history}, last)
            return history['listWindowGap']
        session.add_all([TradeCalendar(exchange='SSE', cal_date=d, is_open=True) for d in (first,last)])
        session.add(Suspension(ts_code=code, trade_date=first, suspend_type='S'))
        assert check() is None
        history['valuations'].insert(0,[first.isoformat(),12,3])
        assert '全天停牌日' in check()
        session.add(DailyPrice(ts_code=code, trade_date=first, close=10, vol=10))
        assert check() is None
        history['valuations'].pop(0)
        assert '缺 1 个' in check()
