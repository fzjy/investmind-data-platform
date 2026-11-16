from datetime import date

from sqlalchemy import select

from app.db.models import FinancialForecast, TradeCalendar
from app.stocks.forecast_events import forecast_charts
from backend.tests.stock_update.test_forecast_details import setup, SYMBOLS


def test_forecasts_require_evidence_for_each_missing_candle(new_pg_factory):
    factory, _ = setup(new_pg_factory)
    july = lambda day: date(2026, 7, day)
    symbol = SYMBOLS[0]
    with factory.begin() as s:
        row = s.scalar(select(FinancialForecast).filter_by(ts_code=symbol,ann_date=july(3)))
        row.ann_date = july(4)
        row.data = {**row.data, "summary":"归母净利润增加", "change_reason":"销量增加"}
        for day in (4, 5):
            s.add(TradeCalendar(exchange="SSE",cal_date=july(day),is_open=False))
    with factory() as s:
        chart = forecast_charts(s, {symbol:[july(3),july(6),july(16)]})[symbol]
        event, = chart["forecastEvents"]
        assert (event["disclosedOn"], event["date"]) == ("2026-07-04", "2026-07-06")
        assert event["forecast"]["change_reason"] == "销量增加"
        assert chart["unplacedForecastCount"] == 1
