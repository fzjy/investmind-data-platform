from datetime import date
from decimal import Decimal as D
from importlib import import_module

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, func, select, text

from app.db.models import MetricInput, MetricValue
from app.stocks.derived.pipeline import DerivedPipeline
from app.stocks.derived.workbench_cards import CARD_IDS, CARD_VERSION, calculate_metric_cards, metric_value_card
from backend.tests.derived.test_pipeline import seed_security


def fixture():
    reports = []
    def add(table, period, values, kind="cumulative"):
        reports.append(dict(table=table, kind=kind, period=period, announcedAt=period,
                            sourceId=f"core.{table}:{len(reports)}", values=values))
    for year in range(2021, 2027):
        for q, suffix in enumerate(("03-31", "06-30", "09-30", "12-31"), 1):
            if year == 2026 and q > 2:
                break
            p = f"{year}-{suffix}"
            profit = D("4.84") if (year, q) == (2025, 4) else D("1.95") if (year, q) == (2024, 4) else D(2)
            add("income", p, {"n_income_attr_p": str(profit), "basic_eps": "0.5"}, "quarter")
            add("income", p, {"n_income_attr_p": 2 * q, "basic_eps": 2})
            add("indicator", p, {"bps": 5, "interestdebt": 7})
    add("balance", "2026-06-30", dict(money_cap=2, st_borr=2, lt_borr=1, st_bonds_payable=1,
                                        bond_payable=1, lease_liab=1, non_cur_liab_due_1y=1))
    years = {str(y): dict(basis='paid-cash-records-v1', querySource='raw.source_payload:1',
        recordedThrough='2026-09-01', covered=True, zeroConfirmed=True, events=[], issues=[], sourceIds=[]) for y in range(2021, 2026)}
    years["2025"].update(zeroConfirmed=False, events=[
        dict(period="2025-06-30", status="paid", payDate="2025-10-02", cashPerShare=".1", sourceIds=["core.dividend:1"]),
        dict(period="2025-12-31", status="paid", payDate="2026-06-30", cashPerShare=".3", sourceIds=["core.dividend:2"]),
        dict(period="2025-12-31", status="pending", payDate="2026-10-02", cashPerShare=".5", sourceIds=["core.dividend:3"])])
    return dict(symbol="600519.SH", asOf="2026-09-01", quote=dict(close="10", tradeDate="2026-09-01",
                totalShare=".01", totalMv=".1", priceSource="core.daily_price:1", basicSource="core.daily_basic:1"),
                financials=reports, dividends=years)


def test_values_and_forecast_evidence():
    inputs = fixture()
    pe, pb, cap, net, fy, count, annual, ntm = calculate_metric_cards(inputs)
    assert [c.value for c in (pe, pb, cap, net, fy, count)] == [5, 2, 1000, -5, 4, 2]
    assert annual.value == D(".1")  # 8 元 / 100 股 × 12.5% / 10 元
    assert ntm.value == D(".171625")  # (2 + 7.73 + 2 + 2) / 100 × 12.5% / 10 × 100
    for card in (annual, ntm):
        assert any("加权派息率" in r["label"] and D(r["value"][:-1]) == D("12.5") for r in card.rows)
    assert any("2026Q4 预测" in r["label"] and r["value"] == "7.73 元" for r in ntm.rows)


def test_no_skip_year_for_missing_ntm_sample_and_excluded_seasonality():
    inputs = fixture()
    inputs["financials"] = [r for r in inputs["financials"] if r["period"] != "2025-12-31" or r["table"] != "income"]
    for row in inputs["financials"]:
        if row["period"] == "2023-06-30" and row["kind"] == "cumulative":
            row["values"]["n_income_attr_p"] = 999
    annual, ntm = calculate_metric_cards(inputs)[-2:]
    assert any("2023 年同期占全年比重（排除" in r["label"] for r in annual.rows)
    assert ntm.value is None and "预测 2026Q4 缺少 2025Q4 单季归母净利" in ntm.missing
    for r in ntm.rows:
        if r["label"] in ("预测归母净利合计", "预测 EPS", "预测每股现金分红"):
            assert r["value"] == "缺失"


def test_unknown_year_and_incomparable_debt():
    inputs = fixture()
    inputs["dividends"]["2024"].update(covered=False, zeroConfirmed=False)
    for row in inputs["financials"]:
        if row["period"] == "2026-06-30" and row["table"] == "indicator":
            row["values"]["interestdebt"] = 12
    inputs["quote"]["totalShare"] = None
    _, _, cap, net, fy, count, annual, ntm = calculate_metric_cards(inputs)
    assert cap.value == 1000 and any("反推总股本" in r["label"] for r in cap.rows)
    assert net.value is None and "有息负债汇总与组成无法勾稽" in net.missing
    assert fy.value == 4
    assert all(c.value is None and any("2024" in m for m in c.missing) for c in (count, annual, ntm))


def test_persistence_rerun_and_legacy_contract(pg_factory):
    factory = pg_factory
    seed_security(factory)
    inputs = fixture()
    for _ in range(2):
        with factory.begin() as session:
            rows = DerivedPipeline.run_metric_cards(session, {inputs["symbol"]: inputs})[inputs["symbol"]]
            assert [r.metric_id for r in rows] == list(CARD_IDS)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(MetricValue)) == 8
        row = session.scalar(select(MetricValue).where(MetricValue.metric_id == CARD_IDS[-1]))
        assert row.formula_version == CARD_VERSION
        assert metric_value_card(row)["value"] == "0.17%"
        lineage = session.scalars(select(MetricInput).where(MetricInput.metric_value_id == row.id)).all()
        assert {"core.income", "core.dividend", "core.daily_basic", "core.daily_price"} <= {r.source_table for r in lineage}
        assert all(len(r.content_hash) == 64 for r in lineage)


def test_upgrade_with_preexisting_rows_and_metadata_bootstrap(pg_url, monkeypatch):
    migration = import_module("migrations.versions.20261111_0019_workbench_card_calculation")
    with create_engine(pg_url).begin() as conn:
        conn.execute(text("create schema derived"))
        conn.execute(text("create table derived.metric_value(id integer primary key)"))
        conn.execute(text("insert into derived.metric_value values (1)"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        migration.upgrade()
        assert conn.execute(text("select id, calculation from derived.metric_value")).one() == (1, None)



def test_provider_multiples_do_not_fill_missing_denominators_and_zero_is_evidenced():
    inputs = fixture()
    inputs["financials"] = []
    inputs["quote"].update(peTtm=17, pb=2)
    inputs["dividends"]["2025"].update(events=[], zeroConfirmed=True)
    pe, pb, _, _, fy, count, *_ = calculate_metric_cards(inputs)
    assert pe.value is pb.value is None
    assert all(any("供应商独立参考值" in r["label"] for r in c.rows) for c in (pe, pb))
    assert fy.value == count.value == 0


def test_adjacent_cumulative_ntm_and_distribution_share_basis():
    inputs = fixture()
    for row in inputs["financials"]:
        if row["period"] == "2025-12-31" and row["table"] == "income":
            row["values"]["n_income_attr_p"] = "10.84" if row["kind"] == "cumulative" else None
    inputs["dividends"]["2025"]["events"][0]["baseShare"] = ".02"
    fy, count, _, ntm = calculate_metric_cards(inputs)[4:]
    assert fy.value == 4
    assert count.value == 2
    assert any("2026Q4 预测" in r["label"] and r["value"] == "7.73 元" for r in ntm.rows)
    inputs["dividends"]["2025"]["events"][0]["stockRatio"] = ".1"
    fy, count, _, _ = calculate_metric_cards(inputs)[4:]
    assert fy.value is None and any("送转" in v for v in fy.missing)
    assert count.value == 2



def test_current_year_stock_leg_applies_on_ex_date_across_fiscal_years():
    from app.stocks.derived.metric_dividends import dividend_years
    from backend.tests.test_dividend_ledger import row

    inputs = fixture()
    cash = row(cash_div_tax="1", stk_div="0", sourceId="raw.source_payload:1:row:1")
    midyear = row(end_date="20260630", ann_date="20260715", imp_ann_date="20260715",
                  record_date="20260820", ex_date="20260821", pay_date=None, div_listdate=None,
                  cash_div_tax="0", stk_div=None, stk_co_rate="1", sourceId="raw.source_payload:1:row:2")
    for day, expected in (("2026-08-20", 10), ("2026-08-21", None), ("2026-11-01", None)):
        inputs["asOf"] = inputs["quote"]["tradeDate"] = day
        years = dividend_years([cash, midyear], date.fromisoformat(day), 1,
            query_coverage=dict(querySource='raw.source_payload:1', sourceIds=['raw.source_payload:1'],
                coverageStart='1990-01-01', coverageEnd=day, issues=[]))
        for entry in inputs['dividends'].values():
            entry['recordedThrough'] = day
        inputs["dividends"].update({y: years[y] for y in ("2025", "2026")})
        pe, _, _, _, fy, count, annual, _ = calculate_metric_cards(inputs)
        assert fy.value == expected and count.value == 1
        assert (pe.value is None) == (expected is None)
        # 2025 DPS 和 2025 EPS 均在这次送转之前，历史派息率仍可核验。
        assert annual.value is not None
        assert years["2026"]["events"][0]["conversionRatio"] == "1"
        if expected is None:
            assert "raw.source_payload:1:row:2" in fy.sources
            assert any("2026-08-21" in reason for reason in fy.missing)


def test_split_between_annual_eps_and_paid_dps_only_blocks_historical_payout():
    inputs = fixture()
    inputs["dividends"]["2025"]["events"] = [dict(
        period="2025-12-31", status="paid", payDate="2026-06-30", recordDate="2026-06-28",
        cashPerShare="1", sourceIds=["core.dividend:2"])]
    # 归属 2025 财年的实施送股当年公告，到账与除权日期分开。
    stock = dict(period="2025-12-31", status="zero", cashPerShare="0", payDate=None,
                 recordDate="2026-05-01", exDate="2026-05-02", bonusRatio="1", sourceIds=["core.dividend:4"])
    inputs["dividends"]["2025"]["events"].append(stock)
    fy, count, annual, ntm = calculate_metric_cards(inputs)[4:]
    assert fy.value == 10 and count.value == 1
    for card in (annual, ntm):
        assert card.value is None and any("年度基本 EPS" in reason for reason in card.missing)
    stock.update(recordDate="2026-10-01", exDate="2026-10-02")
    fy, count, annual, ntm = calculate_metric_cards(inputs)[4:]
    assert fy.value == 10 and annual.value is not None and ntm.value is not None


def test_annual_anchor_skips_single_quarter_only_and_empty_cumulative():
    inputs = fixture()
    inputs["asOf"] = "2026-11-01"
    for entry in inputs['dividends'].values():
        entry['recordedThrough'] = inputs['asOf']
    expected = calculate_metric_cards(inputs)[6].value
    q3 = dict(table="income", kind="quarter", period="2026-09-30", announcedAt="2026-10-31",
              sourceId="core.income:900", values={"n_income_attr_p": 50})
    inputs["financials"].append(q3)
    for additional in ([], [q3 | dict(kind="cumulative", values={}, sourceId="core.income:901")]):
        inputs["financials"].extend(additional)
        card = calculate_metric_cards(inputs)[6]
        assert card.value == expected
        assert {"label": "预测锚点", "value": "2026-06-30"} in card.rows
        assert any("2026Q3 锚点筛选" == item["label"] for item in card.rows)
