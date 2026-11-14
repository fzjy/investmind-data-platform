"""逐财年的已披露范围核验；查询结束不证明完整经济历史。"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from app.portfolio.api.dividends import distribution_date
from app.portfolio.api.dividends import resolve_distribution_evidence
from app.stocks.workbench_enrichment import number


AUDIT_REASONS = {
    'query_not_terminal': '未取得明确的请求分页结束证据',
    'fiscal_year_open': '归属财年尚未结束',
    'no_implemented_events': '没有可核验的实施事件，不按零分红处理',
    'unresolved_source_issue': '存在未解决的修订、取消、日期或数值问题',
    'event_not_paid': '存在未支付或缺支付证据的现金事件',
    'event_source_mismatch': '实施事件与原件版本或税前现金不匹配',
    'implementation_missing': '实施公告日期缺失或尚不可见',
    'event_capital_basis_missing': '事件分红基准股本或基准日期缺失',
    'event_capital_basis_invalid': '事件基准日期晚于资格日或无法核准',
    'cash_evidence_missing': '税前现金或事件身份缺失',
    'capital_conflict': '实施记录的送转股本字段存在冲突',
}

_PROPOSALS = frozenset({'预案', '董事会预案', '股东大会通过'})


def audit_fiscal_disclosures(rows, events, as_of: date, *, query_complete=False):
    """只消费调用方验证过的原件及共享账本事件，不写库或请求供应商。

    disclosedQualified仅表示这批已披露事件可按归属FY核算金额；不承诺
    供应商未漏收、不会晚披露，也不证明跨事件资本连续性。
    """
    def day(value):
        try:
            return distribution_date(value)
        except (ValueError, TypeError):
            return None

    def year(value):
        value = day(value)
        return value[:4] if value else None

    sources = {r['sourceId']:r for r in rows}
    grouped = defaultdict(list)
    years = {y for r in rows if (y := year(r.get('end_date'))) and int(y) <= as_of.year}
    for event in events:
        y = year(event.get('periodEnd'))
        if y and int(y) <= as_of.year:
            grouped[y].append(event)
            years.add(y)
    blocking = set()
    resolved = resolve_distribution_evidence(rows, as_of)
    winners = {(r['end_date'],tuple(r['sourceIds'])) for r in resolved['events']}
    for issue in resolved['issues']:
        if issue['code'] == 'future_date':
            continue
        related = [sources[s] for s in issue['sourceIds'] if s in sources]
        # 解析器先报坏字段、后裁决预案/未来版本，不能让这些诊断污染当前版本。
        def irrelevant(row):
            if row.get('div_proc') in _PROPOSALS:
                return True
            for key in ('ann_date','imp_ann_date'):
                try:
                    value = distribution_date(row.get(key))
                    if value and value > as_of.isoformat():
                        return True
                except (ValueError, TypeError):
                    pass
            return False
        if related and all(irrelevant(r) for r in related):
            continue
        blocking.add(year(issue.get('end_date')))
    result = {}
    for y in sorted(years):
        reasons = set()
        if query_complete is not True:
            reasons.add('query_not_terminal')
        if int(y) >= as_of.year:
            reasons.add('fiscal_year_open')
        if y in blocking or None in blocking:
            reasons.add('unresolved_source_issue')
        if not grouped[y]:
            reasons.add('no_implemented_events')
        for event in grouped[y]:
            if event.get('capitalStatus') == 'conflict':
                reasons.add('capital_conflict')
            if (event.get('periodEnd'),tuple(event.get('sourceIds',[]))) not in winners:
                reasons.add('event_source_mismatch')
            cash = number(event.get('cashPerShare'))
            if cash is None or cash < 0 or not (event.get('recordDate') or event.get('exDate')):
                reasons.add('cash_evidence_missing')
            if event.get('cashStatus') not in ('paid','not_required'):
                reasons.add('event_not_paid')
            linked = [sources[s] for s in event.get('sourceIds',[]) if s in sources]
            if not linked or len(linked) != len(event.get('sourceIds',[])):
                reasons.add('event_source_mismatch')
            for row in linked:
                if cash is None or number(row.get('cash_div_tax')) is None or Decimal(str(cash)) != Decimal(str(row['cash_div_tax'])):
                    reasons.add('event_source_mismatch')
                implementation = day(row.get('imp_ann_date'))
                if not implementation or implementation > as_of.isoformat():
                    reasons.add('implementation_missing')
                if cash is not None and cash > 0:
                    shares = number(row.get('base_share'))
                    baseline = day(row.get('base_date'))
                    if shares is None or shares <= 0 or baseline is None:
                        reasons.add('event_capital_basis_missing')
                    elif baseline > (event.get('recordDate') or event.get('exDate') or ''):
                        reasons.add('event_capital_basis_invalid')
        result[y] = dict(queryComplete=query_complete is True, historyComplete=False,
            disclosedQualified=not reasons, reasons=sorted(reasons),
            reason='；'.join(AUDIT_REASONS[r] for r in sorted(reasons)) or '本次请求已披露实施事件可核算；不证明整财年经济覆盖或资本连续性')
    return result
