from datetime import date, datetime, timezone

from app.db.models import ContentDocument, FinancialIndicator, IncomeStatement
from backend.tests.marts.test_content_runtime import PRODUCT, SYMBOL, _factory, prepare, run, state


def test_conflicting_single_quarter_does_not_fall_back_to_cumulative():
    from types import SimpleNamespace
    from app.content.marts.report_details import quarter_values
    day = date(2026, 4, 24)
    period = date(2026, 3, 31)
    indexed = {period: [(day, SimpleNamespace(report_type=kind, comp_type="1",data={"revenue": value}))
                        for kind, value in [("2", 110), ("2", 111), ("1", 115)]]}
    values, conflicts = quarter_values(indexed, {}, period, day, exact=True)
    assert values["revenue"] is None and "revenue" in conflicts


def test_same_day_reports_republish_with_late_fields_and_original_versions(monkeypatch):
    factory = _factory()
    prepare(factory)
    day = date(2026, 4, 24)
    annual = date(2025, 12, 31)
    first = date(2026, 3, 31)
    titles = ["2025年年度报告", "2026年第一季度报告", "关于2025年年度报告的问询函"]
    with factory.begin() as s:
        for title in titles:
            s.add(ContentDocument(
                source="controlled", content_type="announcement", title=title, dedupe_key=title,
                securities=[SYMBOL], published_at=datetime(2026, 4, 24, 5, tzinfo=timezone.utc),
                source_url="https://example.com/" + str(titles.index(title)), copyright_status="link_only"))
    def notices():
        return {item["title"]: item for item in state(factory)["pointers"][PRODUCT][2]["dailyChart"]["announcements"]}

    assert run(factory, monkeypatch)["status"] == "completed"
    assert notices()[titles[0]]["financialReport"]["metrics"]["revenue"]["value"] is None
    with factory.begin() as s:
        for period, disclosed, revenue, profit in [
            (annual, day, 500, 30), (first, day, 300, 10), (first, date(2026, 5, 1), 999, 999),
        ]:
            s.add(IncomeStatement(ts_code=SYMBOL, end_date=period, ann_date=disclosed, report_type="1",
                                  data={"revenue": revenue, "n_income_attr_p": profit}))
        for period, eps, roe in [(annual, 0.4, 4), (first, 0.6, 5)]:
            s.add(FinancialIndicator(ts_code=SYMBOL, end_date=period, ann_date=day,
                                     data={"q_eps": eps, "q_roe": roe, "roe": 98}))
    assert run(factory, monkeypatch)["status"] == "completed"
    items = notices()
    assert items[titles[0]]["financialReport"]["metrics"]["revenue"]["value"] == 200
    report = items[titles[1]]["financialReport"]
    assert items[titles[1]]["kind"] == "earnings" and report["comparisonPeriod"] == annual.isoformat()
    assert report["metrics"]["revenue"]["changePercent"] == 50
    assert report["metrics"]["n_income_attr_p"]["reason"] == "上季为零"
    assert report["metrics"]["q_eps"]["changePercent"] == 50
    assert report["metrics"]["q_roe"]["changePoints"] == 1
    assert report["metrics"]["q_roe"]["changePercent"] == 25
    assert "financialReport" not in items[titles[2]]
