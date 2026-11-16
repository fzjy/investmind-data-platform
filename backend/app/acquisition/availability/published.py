from dataclasses import replace
from sqlalchemy import select
from app.acquisition.availability.stored import finite, MISSING, UNKNOWN
from app.acquisition.contracts import Availability
from app.db.models import PageSnapshot, PublishedSnapshot, StockWorkbenchSnapshot


def snapshot(session, context):
    product, version = context.product, context.version
    pointer = session.get(PublishedSnapshot, product)
    if pointer and (not version or pointer.version == version):
        return pointer.version, pointer.payload
    if version:
        saved = session.scalar(select(PageSnapshot).where(PageSnapshot.product == product, PageSnapshot.snapshot_version == version))
        if saved:
            return version, saved.payload
        if product.startswith('stock-workbench:'):
            saved = session.scalar(select(StockWorkbenchSnapshot).where(
                StockWorkbenchSnapshot.ts_code == product.split(':',1)[1], StockWorkbenchSnapshot.snapshot_version == version))
            if saved:
                return version, saved.payload
    raise KeyError('发布产品或版本不存在')


def _item(key, product, payload, period):
    if product == 'stock-workbench:'+key.subject_key and key.entry_id == 'stock-daily':
        for candle in reversed((payload.get('dailyChart') or {}).get('candles',[])):
            day = candle.get('date')
            if (not period or day == period) and finite(candle.get('close')):
                return Availability('available',day,'该版本中存在日行情')
        return MISSING
    if product == 'macro-insights' and key.subject_kind == 'series':
        series = next((s for s in payload.get('sandboxIndicators',[]) if s.get('id')==key.subject_key),None)
        if series is not None:
            points = [p for p in series.get('values',[]) if finite(p.get('value')) and (not period or p.get('month')==period)]
            if not points:
                return MISSING
            latest = max(points,key=lambda p:p.get('month',''))
            meta = next((r for r in payload.get('seriesStatus',[]) if r.get('id')==key.subject_key),{})
            if not meta.get('actualSource') or meta.get('observedAt')!=latest.get('month'):
                return Availability('unknown',reason='指标来源或数据期与快照元数据不匹配')
            return Availability('available',latest['month'],'指定快照中存在有效值',meta['actualSource'])
        derived = next((s for s in (payload.get('derivedInsights') or {}).get('series',[])
                        if s.get('id')==key.subject_key),None)
        if derived is not None:
            points = [p for p in derived.get('points',[]) if finite(p.get('value')) and (not period or p.get('month')==period)]
            return Availability('available',points[-1].get('month'),'快照中的计算结果可用') if points else MISSING
        return UNKNOWN if key.entry_id.startswith('macro-extension-') else MISSING
    if product == 'stock-workbench:'+key.subject_key:
        if payload.get('symbol',key.subject_key) != key.subject_key:
            return Availability('unknown',reason='快照证券与请求对象不一致')
        if key.entry_id == 'stock-announcements':
            events = (payload.get('dailyChart') or {}).get('announcements',[])
            return Availability('available',reason='该证券的快照内有公告') if events else MISSING
        if key.entry_id == 'stock-ratio-inputs':
            metrics = payload.get('metrics') or {}
            field = next((f for f in metrics.get('fields',[]) if f['id']==key.item_id),{})
            points = [(p,v) for p,v in zip(metrics.get('years',[]),field.get('values',[]))
                      if finite(v) and (not period or p==period)]
            return Availability('available',points[-1][0],'指定年报字段可用') if points else MISSING
        if key.entry_id == 'stock-dividend-yield' or key.entry_id.startswith(('metric-','card-')):
            from app.stocks.marts.screener_list import YIELD_CARDS
            lookup = YIELD_CARDS[key.item_id] if key.entry_id=='stock-dividend-yield' else key.entry_id.split('-',1)[1]
            card = next((c for c in payload.get('metrics',{}).get('cards',[]) if c.get('id',c.get('label'))==lookup),{})
            value = str(card.get('value','')).removesuffix('%').removesuffix('亿元').removesuffix('次').strip()
            return Availability('available',reason='指标卡包含有效数值') if finite(value) else MISSING
    if product == 'screener' and (key.entry_id.startswith('screener-') or key.entry_id=='stock-dividend-yield'):
        row = next((r for r in payload.get('stocks',[]) if r.get('symbol') == key.subject_key),{})
        value = (row.get('researchValues') or {}).get(key.item_id, row.get(key.item_id))
        return Availability('available',reason='发布行中有有效数值') if finite(value) else MISSING
    if key.entry_id == 'security-search' and product == 'screener':
        return Availability('available',reason='快照内包含可检索的证券') if payload.get('stocks') else MISSING
    if key.entry_id == 'macro-spreads' and product == 'macro-insights':
        series = (payload.get('derivedInsights') or {}).get('series',[])
        return Availability('available',reason='快照内有差值结果') if any(finite(p.get('value')) for s in series for p in s.get('points',[])) else MISSING
    if key.entry_id in ('market-overview','market-explorer','portfolio-inputs'):
        expected = {'market-overview':'market-overview','market-explorer':'market-explorer','portfolio-inputs':'portfolio'}[key.entry_id]
        if product == expected:
            fields = {'portfolio-inputs':('prices','assets'),'market-overview':('marketSnapshot','breadthSnapshots'),
                      'market-explorer':('heatmapData','rankingLeaders')}[key.entry_id]
            return Availability('available',reason='指定产品的业务内容可用') if all(payload.get(f) for f in fields) else MISSING
    return UNKNOWN


def read_published(session, keys, context):
    version, payload = snapshot(session, context)
    return {key:replace(_item(key,context.product,payload,context.period),version=version) for key in keys}
