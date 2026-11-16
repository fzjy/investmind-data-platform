from decimal import Decimal, InvalidOperation

from sqlalchemy import select, or_, cast, func
from sqlalchemy.dialects.postgresql import JSONB
from app.acquisition.periods import bounds

from app.acquisition.contracts import Availability
from app.db import models as db


def finite(value):
    if isinstance(value, bool) or value is None:
        return False
    try:
        return Decimal(str(value)).is_finite()
    except (InvalidOperation, ValueError, TypeError):
        return False


MISSING = Availability('missing', reason='当前范围内没有可用记录')
UNKNOWN = Availability('unknown', reason='尚无法将该条目绑定到本次查询的数据范围')

# 主体、数据期及代表该条目可用的业务值。事件记录的日期本身具有业务含义。
STOCK_COLUMNS = {
    'stock-daily': (db.DailyPrice,'trade_date','close'),
    'stock-adjustment': (db.AdjustmentFactor,'trade_date','adj_factor'),
    'stock-daily-basic': (db.DailyBasic,'trade_date','total_mv'),
    'stock-financial-indicator': (db.FinancialIndicator,'end_date','data'),
    'stock-balance': (db.BalanceSheet,'end_date','data'),
    'stock-cashflow': (db.CashflowStatement,'end_date','data'),
    'stock-income': (db.IncomeStatement,'end_date','data'),
    'stock-forecast': (db.FinancialForecast,'end_date','data'),
    'stock-express': (db.FinancialExpress,'end_date','data'),
    'stock-holder-count': (db.ShareholderCount,'end_date','holder_num'),
    'stock-dividend': (db.Dividend,'end_date','div_proc'),
    'stock-disclosure': (db.DisclosureSchedule,'end_date','pre_date'),
    'stock-top-holders': (db.TopHolder,'end_date','holder_name'),
    'stock-repurchase': (db.Repurchase,'ann_date','proc'),
    'stock-unlock': (db.ShareUnlock,'ann_date','float_date'),
    'stock-business': (db.BusinessSegment,'end_date','revenue'),
}


def _available(period=None, source=None):
    from app.acquisition.adapters.source_ids import public_source
    return Availability('available', str(period) if period is not None else None, '已有可用记录', public_source(source) if source else None)


def _stored(session, key, now, selected=None):
    def during(column):
        return column.between(*bounds(selected)) if selected else True
    code, entry = key.subject_key, key.entry_id
    from app.acquisition.adapters.aliases import SERIES
    mapped = SERIES.get((entry,code))
    if key.subject_kind == 'series' or mapped:
        row = session.execute(select(db.MacroObservation.value, db.MacroObservation.observed_at, db.MacroObservation.source)
            .where(db.MacroObservation.series_id == (mapped or code),
                   db.MacroObservation.source == 'tushare' if mapped else True, db.MacroObservation.released_at <= now,
                   or_(db.MacroObservation.valid_until.is_(None),db.MacroObservation.valid_until > now),
                   during(db.MacroObservation.observed_at))
            .order_by(db.MacroObservation.observed_at.desc(), db.MacroObservation.released_at.desc()).limit(1)).first()
        return _available(row[1], row[2]) if row and finite(row[0]) else MISSING
    if entry in STOCK_COLUMNS:
        model, period, field = STOCK_COLUMNS[entry]
        column = getattr(model,field)
        if key.item_id == 'limit_status':
            column = model.data['limit_status'].as_integer()
        rows = session.execute(select(column,getattr(model,period)).where(
            model.ts_code == code, during(getattr(model,period)), column.is_not(None)).order_by(getattr(model,period).desc()).limit(1)).first()
        if not rows:
            return MISSING
        value = rows[0]
        if field == 'data' and not any(finite(v) for k,v in (value or {}).items()
                                      if not k.endswith(('_date','_code','_type','_flag')) and k not in ('ts_code','currency','year','quarter')):
            return MISSING
        return _available(rows[1],'tushare') if value is not None else MISSING
    if entry in ('stock-basic','security-region'):
        stock = session.get(db.Security, code)
        value = stock and (stock.name if entry == 'stock-basic' else (stock.data or {}).get('area'))
        return _available(source='tushare') if value else MISSING
    if entry == 'stock-classification':
        name = session.scalar(select(db.Classification.name).where(db.Classification.source == code).limit(1))
        return _available(source='tushare') if name else MISSING
    if entry == 'stock-classification-members':
        day = session.scalar(select(db.ClassificationMember.effective_from).where(db.ClassificationMember.ts_code == code)
                             .order_by(db.ClassificationMember.effective_from.desc()).limit(1))
        return _available(day,'tushare') if day else MISSING
    if entry.startswith(('metric-','card-')) or entry == 'stock-dividend-yield':
        from app.stocks.marts.screener_list import YIELD_CARDS
        metric = YIELD_CARDS[key.item_id] if entry=='stock-dividend-yield' else entry.split('-',1)[1]
        row = session.scalar(select(db.MetricValue).where(db.MetricValue.ts_code == code,
            db.MetricValue.metric_id == metric,during(db.MetricValue.as_of)).order_by(db.MetricValue.as_of.desc(), db.MetricValue.calculated_at.desc()).limit(1))
        if not row or not finite(row.value) or row.missing_fields:
            return MISSING
        lineage = session.scalar(select(db.MetricInput.id).where(db.MetricInput.metric_value_id==row.id,
            db.MetricInput.source_record_key!='').limit(1))
        return _available(row.as_of) if lineage else Availability('unknown',reason='计算结果缺少输入记录引用')
    if entry.startswith('risk-'):
        row = session.scalar(select(db.RiskResult).where(db.RiskResult.ts_code == code,
            db.RiskResult.risk_id == entry[5:], db.RiskResult.snapshot_version.is_not(None)).limit(1))
        return _available(row.as_of) if row and row.status != 'unknown' and not row.missing_fields else MISSING
    if entry in ('etf-daily','index-global','index-daily'):
        api = {'etf-daily':'fund_daily','index-global':'index_global','index-daily':'index_daily'}[entry]
        row = session.scalar(select(db.MarketDailyQuote).where(db.MarketDailyQuote.asset_code == code,
            db.MarketDailyQuote.api_name == api,during(db.MarketDailyQuote.trade_date)).order_by(db.MarketDailyQuote.trade_date.desc()).limit(1))
        return _available(row.trade_date,'tushare') if row and finite(row.close) else MISSING
    if entry == 'stock-announcements':
        model = db.ContentDocument
        if session.get_bind().dialect.name == 'postgresql':
            linked = cast(model.securities,JSONB).contains([code])
        else:
            symbols = func.json_each(model.securities).table_valued('value')
            linked = select(1).select_from(symbols).where(symbols.c.value == code).exists()
        q = select(model.published_at).where(linked,model.content_type=='announcement',
            model.source=='cninfo',model.published_at <= now)
        if selected:
            from datetime import datetime, time, timedelta
            from zoneinfo import ZoneInfo
            low, high = bounds(selected)
            zone = ZoneInfo('Asia/Shanghai')
            q = q.where(model.published_at >= datetime.combine(low,time.min,zone),
                        model.published_at < datetime.combine(high+timedelta(days=1),time.min,zone))
        day = session.scalar(q.order_by(model.published_at.desc()).limit(1))
        return _available(day.date(),'cninfo') if day else MISSING
    if entry == 'news':
        from app.db.models.news import NewsDelivery
        day = session.scalar(select(NewsDelivery.fetched_at).where(NewsDelivery.source == code)
                             .order_by(NewsDelivery.fetched_at.desc()).limit(1))
        return _available(day,code) if day else MISSING
    if entry.startswith('external-'):
        kind = entry.removeprefix('external-')
        if code.startswith('source-sha256-'):
            from app.acquisition.repository import StateRepository
            from app.acquisition.adapters.source_ids import public_source
            proof = StateRepository(session).successes((key,)).get(key)
            file_ids = [r['fileId'] for r in proof.references if 'fileId' in r] if proof else []
            sources = session.execute(select(db.ExternalRecord.payload['source'].as_string(),db.ExternalFile.imported_at)
                .join(db.ExternalFile,db.ExternalRecord.external_file_id==db.ExternalFile.id)
                .where(db.ExternalFile.id.in_(file_ids),db.ExternalFile.status=='imported',db.ExternalRecord.record_type==kind,
                db.ExternalRecord.payload['contentType'].as_string()==key.item_id if key.item_id else True))
            return next((_available(at,code) for source,at in sources if source and public_source(source)==code),MISSING)
        imported = session.scalar(select(db.ExternalFile.imported_at).join(db.ExternalRecord,
            db.ExternalRecord.external_file_id == db.ExternalFile.id).where(db.ExternalFile.status == 'imported',
            db.ExternalRecord.record_type == kind,
            db.ExternalRecord.payload['contentType'].as_string()==key.item_id if key.item_id else True,
            db.ExternalRecord.payload['source'].as_string() == code)
            .order_by(db.ExternalFile.imported_at.desc()).limit(1))
        return _available(imported,code) if imported else MISSING
    if entry == 'trade-calendar':
        day = session.scalar(select(db.TradeCalendar.cal_date).where(db.TradeCalendar.exchange == code,during(db.TradeCalendar.cal_date))
                             .order_by(db.TradeCalendar.cal_date.desc()).limit(1))
        return _available(day,'tushare') if day else MISSING
    return UNKNOWN


def read_stored(session, keys, *, now, period=None):
    from app.acquisition.contracts import AuditContext, UnknownResource
    from app.acquisition.availability.published import read_published
    from app.acquisition.repository import StateRepository
    from app.acquisition.availability.receipts import raw_availability
    results = {key:_stored(session,key,now,period) for key in keys}
    groups = {}
    products = {'portfolio-inputs':'portfolio','market-overview':'market-overview','market-explorer':'market-explorer',
                'macro-spreads':'macro-insights','macro-enterprise':'macro-transmission','security-search':'screener'}
    for key in keys:
        product = products.get(key.entry_id)
        if key.entry_id.startswith('screener-'):
            product = 'screener'
        elif key.entry_id in ('stock-ratio-inputs','stock-research'):
            product = 'stock-workbench:'+key.subject_key
        elif key.subject_kind=='series' and key.entry_id.endswith(('spread','minus_m2_yoy')):
            product = 'macro-insights'
        if product:
            groups.setdefault(product,[]).append(key)
    for product, members in groups.items():
        try:
            results.update(read_published(session,members,AuditContext('published',product,period=period)))
        except UnknownResource:
            results.update(dict.fromkeys(members,MISSING))
    raw_keys = tuple(key for key in keys if results[key].kind in ('missing','unknown') and
                     (key.entry_id in ('stock-business','stock-dividend','stock-announcements','index-basic','index-weights',
                                      'etf-basic','etf-shares','fx-daily','market-margin','market-sz-summary','market-daily-summary',
                                      'stock-bse-mapping','source-cn_gdp','news') or key.entry_id.startswith('commodity-')))
    for key,proof in StateRepository(session).successes(raw_keys).items():
        value = raw_availability(session,key,proof)
        if value is not None:
            results[key] = value
    return results
