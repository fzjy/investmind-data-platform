"""在已发布行情的日期范围内定位带披露日期的结构化预告。"""
from copy import deepcopy
from collections import defaultdict
from datetime import date

from sqlalchemy import select

from app.content.api.read_models import WorkbenchContent, load_workbench_content
from app.db.models import FinancialForecast, PublishedSnapshot
from app.shared.trading_days import candle_dates, load_trading_days
from app.jobs.locks import data_publication_lock_key, job_lock_for_engine
from app.marts.contracts import MartCandidate, MartRows, QualityContext, release_version
from app.marts.publication import SnapshotPublisher
from app.marts.quality import QualityGate


def forecast_charts(session, prices, *, trading_days=None):
    trading = trading_days if trading_days is not None else load_trading_days(session, prices)
    if not trading:
        return {}
    first = min(days.first for days in trading.values())
    last = max(days.last for days in trading.values())
    result = {}
    for r in session.scalars(select(FinancialForecast).where(FinancialForecast.ts_code.in_(trading),
            FinancialForecast.ann_date.between(first, last)).order_by(FinancialForecast.ann_date, FinancialForecast.id)):
        symbol = r.ts_code
        days = trading[symbol]
        if not days.first <= r.ann_date <= days.last:
            continue
        placed, _, _ = days.locate(r.ann_date)
        chart = result.setdefault(symbol, {"forecastEvents": [], "unplacedForecastCount": 0})
        if placed is None:
            chart["unplacedForecastCount"] += 1
            continue
        data = r.data
        chart["forecastEvents"].append(dict(id=f"forecast-{r.id}", kind="earnings", label="业绩预告",
            date=placed.isoformat(), disclosedOn=r.ann_date.isoformat(), reportPeriod=r.end_date.isoformat(),
            forecast={key:data.get(key) for key in ("type","p_change_min","p_change_max","net_profit_min",
                "net_profit_max","summary","change_reason")}))
    return result


def publish_forecast_charts(factory, symbols, *, include_announcements=False):
    lock = job_lock_for_engine(factory.kw["bind"])
    key = data_publication_lock_key()
    if not lock.try_acquire(key):
        return {"status":"locked"}
    try:
        with factory() as s:
            rows = s.scalars(select(PublishedSnapshot).where(PublishedSnapshot.product.in_(
                ["stock-workbench:" + symbol for symbol in symbols]))).all()
            prices = {r.payload["symbol"]: candle_dates(r.payload["dailyChart"]["candles"])
                for r in rows}
            trading = load_trading_days(s, prices)
            charts = forecast_charts(s, prices, trading_days=trading)
            content_by_symbol = {}
            if include_announcements:
                by_date = defaultdict(list)
                for symbol, days in prices.items():
                    if days:
                        by_date[max(days)].append(symbol)
                for cutoff, group in by_date.items():
                    content_by_symbol.update(load_workbench_content(s, group, cutoff, trading))
            candidates, versions, freshness = [], {}, {}
            for r in rows:
                payload = deepcopy(r.payload)
                chart = payload["dailyChart"]
                if include_announcements:
                    symbol = payload["symbol"]
                    if prices[symbol]:
                        content = content_by_symbol.get(symbol, WorkbenchContent())
                        chart["announcements"] = content.announcements
                        if content.coverage:
                            chart["announcementCoverage"] = content.coverage
                        payload["quote"].update(mentions=content.mentions, latestMention=content.latest_mention)
                chart.pop("forecastEvents", None)
                chart.pop("unplacedForecastCount", None)
                chart.update(charts.get(payload["symbol"], {}))
                if payload == r.payload:
                    continue
                versions[r.product] = r.version
                freshness[r.product] = r.stale, r.stale_reason
                candidates.append(MartCandidate(r.product, r.as_of, payload,
                    MartRows(workbenches=(payload,), charts=tuple(chart["candles"])), stale=r.stale, stale_reason=r.stale_reason))
            from app.stocks.marts.risk_cards import project_overlay_risks
            candidates = project_overlay_risks(s, candidates, versions)
        if not candidates:
            return {"status":"no_change","promotedProducts":[]}
        checks = QualityGate().evaluate(candidates, QualityContext(0, tuple(c.payload["symbol"] for c in candidates), 0, 0, ()))
        version = release_version(max(c.as_of for c in candidates), candidates)
        result = SnapshotPublisher(factory).publish_many(candidates, version, checks,
            expected_versions=versions, expected_freshness=freshness, preserve_freshness_check=True)
        return {"status":"completed","promotedProducts":list(result.promoted_products)}
    finally:
        lock.release(key)
