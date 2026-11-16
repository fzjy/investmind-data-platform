"""从选定快照中读取聚合实际涵盖的对象。"""
from functools import lru_cache
from app.acquisition.contracts import AuditContext, StateKey, UnknownResource
from app.acquisition.availability.published import snapshot


def member_reader(session,context):
    @lru_cache(maxsize=8)
    def payload(product):
        if context.view == 'published' and context.product != product:
            return {}
        try:
            return snapshot(session,AuditContext('published',product,context.version))[1]
        except UnknownResource:
            return {}

    def members(key,dep):
        if dep.subject_kind == 'series':
            return (dep.item_id or dep.entry_id,)
        if key.subject_kind == dep.subject_kind:
            return (key.subject_key,)
        if dep.entry_id=='stock-classification' and key.subject_kind=='security':
            return ('SW2021',)
        if key.entry_id=='portfolio-inputs' and dep.subject_kind=='security':
            return tuple(a['code'] for a in payload('portfolio').get('assets',[])
                         if a.get('type')=='stock' and a.get('code'))
        if key.entry_id=='security-search' and dep.subject_kind=='security':
            return tuple(s['symbol'] for s in payload('screener').get('stocks',[]) if s.get('symbol'))
        if key.entry_id=='market-explorer' and dep.subject_kind=='security':
            sectors=(payload('market-explorer').get('heatmapData') or {}).get('majorIndustry',[])
            return tuple(sorted({s['symbol'] for sector in sectors for s in sector.get('stocks',[]) if s.get('symbol')}))
        return ()

    def enterprise_inputs(key):
        analysis = payload('macro-transmission').get('enterpriseAnalysis') or {}
        targets = (analysis.get('officialTargets') or {}).get('targets',[])
        target = next((t for t in targets if t.get('code')==key.item_id),{})
        from app.macro.enterprise import CARD_QUALITY_FIELDS
        needed = set()
        table_ids = {'income':'stock-income','balance':'stock-balance','cashflow':'stock-cashflow','daily_basic':'stock-daily-basic'}
        cards = (target.get('analysis') or {}).get('cards',[])
        periods = {p['period'] for c in cards for p in c.get('points',[]) if p.get('period')}
        from app.acquisition.periods import contains
        selected = max((day for day in periods if not context.period or contains(context.period,day)),default=None)
        for card in cards:
            quality = next((q for q in card.get('quality',[]) if q.get('period')==selected),{})
            fields = CARD_QUALITY_FIELDS.get(card.get('id'),())
            for symbol in quality.get('included',[]):
                for field in fields:
                    needed.add(StateKey(table_ids[field.split('.')[0]],'','security',symbol,key.scope_kind))
        return sorted(needed)
    return members,enterprise_inputs
