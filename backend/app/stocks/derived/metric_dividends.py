"""把账本裁决投影到归属财年，保留可追溯的已付、待付和排除证据。"""
from collections import defaultdict
import binascii
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select

from app.connectors.tushare.contracts import request_hash
from app.db.models import Dividend, IngestionBatch, Security, SourcePayload
from app.ingestion.repositories import content_hash
from app.portfolio.api.dividends import (DIVIDEND_FIELDS, distribution_date, dividend_metadata,
    resolve_distribution_evidence, verified_dividend_rows)


PAID_CASH_BASIS = 'paid-cash-records-v1'


def query_issue_message(issue):
    """采集机器状态在读模型边界转换为可定位的中文原因。"""
    code, _, symbol = issue.partition(':')
    labels = {'query_missing': '分红尚未采集', 'query_failed': '分红查询失败',
              'query_pending': '分红查询尚未完成', 'query_running': '分红查询进行中',
              'query_blocked': '分红查询受阻', 'query_invalid': '分红查询原件校验失败',
              'query_truncated': '分红查询响应被截断', 'truncated': '分红查询响应被截断',
              'missing_listing_date': '缺少上市日期，无法确定可统计历史',
              'mapping_query_missing': '证券代码变更对照尚未取得',
              'mapping_query_failed': '证券代码变更对照查询失败',
              'mapping_source_invalid': '证券代码变更对照原件校验失败',
              'mapping_query_pending': '证券代码变更对照尚未采集完成',
              'alias_query_failed': '证券历史代码分红查询失败'}
    if code in labels:
        return (symbol + ' ' if symbol else '') + labels[code]
    if code.startswith('mapping_'):
        return '证券代码变更对照校验失败（' + issue + '）'
    if code.startswith('query_'):
        return (symbol + ' ' if symbol else '') + '分红查询未成功完成（' + code + '）'
    if code.startswith('DIVIDEND_'):
        return '分红原件校验失败（' + issue + '）'
    return issue


def paid_cash_year_available(entry, as_of=None):
    """旧 covered/zeroConfirmed 不构成新口径的零值证据。"""
    return (entry.get('basis') == PAID_CASH_BASIS and bool(entry.get('covered'))
            and bool(entry.get('querySource')) and bool(entry.get('recordedThrough'))
            and not entry.get('issues')
            and (as_of is None or entry['recordedThrough'] == str(as_of)))


def _year(cutoff, query_source):
    return dict(events=[], issues=[], covered=False, zeroConfirmed=False, sourceIds=[],
                observations=[], basis=PAID_CASH_BASIS, querySource=query_source,
                recordedThrough=cutoff, coverageBasis='缺少成功的逐证券全历史分红查询')


def _normalized(row):
    item = dict(row)
    for field in DIVIDEND_FIELDS:
        value = item.get(field)
        if field.endswith('_date') or field == 'div_listdate':
            try:
                item[field] = distribution_date(str(value)) if value not in ('', None) else None
            except ValueError:
                item[field] = str(value)
        elif field not in ('ts_code', 'div_proc'):
            try:
                number = format(Decimal(str(value)), 'f') if value not in ('', None) else None
                item[field] = number.rstrip('0').rstrip('.') if number and '.' in number else number
            except InvalidOperation:
                item[field] = str(value)
    return item


def dividend_years(rows, as_of, verified_raw_id=None, *, history_years=5, query_coverage=None, listed_at=None):
    cutoff = as_of.isoformat()
    # 当前财年可能已发生中期送转，影响旧 DPS/EPS 与行情日股价的可比性。
    rows = list(rows)
    query = query_coverage or {}
    try:
        listed = distribution_date(listed_at) if listed_at else None
        coverage_start = distribution_date(query.get('coverageStart'))
        coverage_end = distribution_date(query.get('coverageEnd'))
    except ValueError:
        listed = coverage_start = coverage_end = None
    start = as_of.year - history_years if history_years is not None else min(
        [as_of.year, *[int(str(r.get('end_date') or '')[:4]) for r in rows
                      if str(r.get('end_date') or '')[:4].isdigit()]])
    if history_years is None and listed:
        start = min(start, int(listed[:4]))
    years = {str(y): _year(cutoff, query.get('querySource')) for y in range(start, as_of.year + 1)}
    for entry in years.values():
        entry['listedAt'] = listed
        entry['queryCoverage'] = deepcopy(query)
    normalized = [_normalized(r) for r in rows]
    def already_visible(value):
        try:
            return not value or distribution_date(value) <= cutoff
        except ValueError:
            return True  # 保留无效日期交给账本标缺。
    visible = [r for r in normalized if already_visible(r['ann_date']) and already_visible(r['imp_ann_date'])]
    raw_prefix = f'raw.source_payload:{verified_raw_id}:row:' if verified_raw_id is not None else None
    # Core和例行同步使用窄合同。若同版已有全字段原件，核对两边都有的字段，
    # 相符时只使用原件；相异时保留两条，让账本输出冲突。
    originals = defaultdict(list)
    # 每一份已验证的完整原件都带有逐行引用。反复捕获相同事实取首次证据，
    # 当前原件也含该事实时，可作为当前供应商响应覆盖检查的一部分。
    unique_originals = {}
    observations = []
    for r in visible:
        current = raw_prefix and r['sourceId'].startswith(raw_prefix)
        if r.get('_verifiedOriginal') or current:
            signature = tuple(r.get(k) for k in DIVIDEND_FIELDS)
            if signature not in unique_originals:
                unique_originals[signature] = r
                observations.append(r)
        else:
            observations.append(r)
    def identity(r):
        return tuple(r.get(k) for k in ('end_date', 'ann_date', 'div_proc'))
    for r in unique_originals.values():
        originals[identity(r)].append(r)
    retained = []
    for r in observations:
        year = (r['end_date'] or '')[:4]
        if year not in years:
            if (not r['end_date'] or not year.isdigit()) and r.get('div_proc') in ('实施', '实施完成'):
                for entry in years.values():
                    entry['issues'].append('分配记录缺少可识别的归属财年')
                    entry['sourceIds'].append(r['sourceId'])
                    # 历史重算必须保留问题所依附的公开版本，不能把最终缺项回灌到公告前。
                    entry['observations'].append({k: r.get(k) for k in
                        (*DIVIDEND_FIELDS, 'sourceId', '_verifiedOriginal', '_originalTsCode')})
            continue
        entry = years[year]
        entry['sourceIds'].append(r['sourceId'])
        entry['observations'].append({k: r.get(k) for k in
            (*DIVIDEND_FIELDS, 'sourceId', '_verifiedOriginal', '_originalTsCode')})
        matches = originals[identity(r)]
        if r not in matches and any(all(r.get(k) == other.get(k) for k in DIVIDEND_FIELDS
                                     if r.get(k) is not None) for other in matches):
            continue
        retained.append(r)
    # 预案及股东会通过阶段退出实付统计，包括这些阶段中的坏字段。
    retained = [r for r in retained if r.get('div_proc') in
                ('实施', '实施完成', '取消', '停止', '停止实施', '终止实施', '取消实施', '不实施')]
    implemented_years = {(r['end_date'] or '')[:4] for r in retained if r['div_proc'] in ('实施', '实施完成')}
    retained = [r for r in retained if r['div_proc'] in ('实施', '实施完成')
                or (r['end_date'] or '')[:4] in implemented_years]
    resolved = resolve_distribution_evidence(retained, as_of, protect_paid=True)
    for event in resolved['events']:
        year = event['end_date'][:4]
        entry = years[year]
        cash, pay = event['cash_div_tax'], event['pay_date']
        status = ('zero' if cash == 0 else 'missing' if cash is None or pay is None
                  else 'pending' if pay > cutoff else 'paid')
        entry['events'].append(dict(period=event['end_date'], payDate=pay, announcedAt=event['ann_date'],
            cashPerShare=str(cash) if cash is not None else None, status=status, sourceIds=event['sourceIds'],
            recordDate=event['record_date'], exDate=event['ex_date'], baseDate=event['base_date'],
            stockRatio=str(event['stk_div']) if event['stk_div'] is not None else None,
            bonusRatio=str(event['stk_bo_rate']) if event['stk_bo_rate'] is not None else None,
            conversionRatio=str(event['stk_co_rate']) if event['stk_co_rate'] is not None else None,
            baseShare=str(event['base_share']) if event['base_share'] is not None else None))
        if status == 'missing':
            entry['issues'].append('已实施分配缺少税前每股现金或派息日')
    messages = {'paid_revision_conflict': '已派息记录与后续更正或撤回冲突',
                'same_day_conflict': '同一版本的分配原始记录相互冲突',
                'unlocated_cancellation': '无法定位撤回的分配方案',
                'missing_event_identity': '分配记录缺少登记日与除权日，无法确认事件身份'}
    by_source = {r['sourceId']: r for r in retained}
    for issue in resolved['issues']:
        year = (issue.get('end_date') or '')[:4]
        if issue['code'] in ('future_date', 'stage_not_implemented'):
            continue
        sources = [by_source[s] for s in issue['sourceIds']]
        if year not in years:
            if not year:
                for entry in years.values():
                    entry['issues'].append(messages.get(issue['code'], '分配数据待核验（' + issue['code'] + '）'))
            continue
        if (issue['code'] == 'missing_event_identity' and sources
            and all(source['cash_div_tax'] == '0' for source in sources)):
            continue
        years[year]['issues'].append(messages.get(issue['code'], '分配数据待核验（' + issue['code'] + '）'))
    for y, entry in years.items():
        entry['queryIssues'] = list(query.get('issues', []))
        entry['issues'] = sorted(set([*entry['issues'], *map(query_issue_message, entry['queryIssues'])]))
        entry['observations'].sort(key=lambda r: r['sourceId'])
        entry['sourceIds'] = sorted(set([*entry['sourceIds'], *query.get('sourceIds', [])]))
        needed_start = max(f'{y}-01-01', listed or f'{y}-01-01')
        in_listing = not listed or int(y) >= int(listed[:4])
        query_valid = bool(query.get('querySource') and query.get('sourceIds')
                           and coverage_start and coverage_end and coverage_start <= needed_start
                           and needed_start <= cutoff <= coverage_end)
        entry['queryCovered'] = query_valid and in_listing and not query.get('issues')
        entry['covered'] = entry['queryCovered'] and not entry['issues']
        if entry['covered']:
            entry['coverageBasis'] = f'截至 {cutoff}，按成功的全历史查询累计实际已派现金'
            entry['zeroConfirmed'] = not any(e['status'] == 'paid' for e in entry['events'])
        elif not in_listing:
            entry['coverageBasis'] = f'该财年早于上市日期 {listed}'
        elif query.get('issues'):
            entry['coverageBasis'] = '分红查询缺项：' + '；'.join(map(query_issue_message, query['issues']))
        elif query.get('querySource') and not query_valid:
            entry['coverageBasis'] = '分红查询期间未覆盖该财年至所选截止日，需刷新或补齐历史'
        elif entry['issues']:
            entry['coverageBasis'] = '实际派息记录存在待核验缺项'
    return years


def _sync_rows(session, symbols):
    # 日常股票同步只把实施记录转入 Core；其余阶段在同批已验收的窄字段响应中。
    from app.connectors.tushare.strategies import Dataset
    from app.stocks.update.planning import plan_for
    fields = plan_for(Dataset.DIVIDEND, {}).fields
    hashes = {request_hash('dividend', {'ts_code': s}, fields): s for s in symbols}
    distinct_responses = select(func.min(SourcePayload.id)).join(IngestionBatch, IngestionBatch.id == SourcePayload.batch_id).where(
        SourcePayload.api_name == 'dividend', SourcePayload.request_hash.in_(hashes),
        IngestionBatch.dataset == Dataset.DIVIDEND.value, IngestionBatch.status == 'completed',
        IngestionBatch.source == 'tushare', IngestionBatch.rejected_count == 0).group_by(
            SourcePayload.request_hash, SourcePayload.content_hash)
    seen = set()
    for raw in session.scalars(select(SourcePayload).where(SourcePayload.id.in_(distinct_responses)).order_by(SourcePayload.id)):
        payload = raw.payload
        actual_fields = payload.get('fields', [])
        if raw.content_hash != content_hash(payload) or not set(fields) <= set(actual_fields):
            raise ValueError('DIVIDEND_SYNC_SOURCE_INVALID')
        for i, values in enumerate(payload['items'], 1):
            row = dict(zip(actual_fields, values, strict=True))
            if row['ts_code'] != hashes[raw.request_hash]:
                raise ValueError('DIVIDEND_SYNC_SYMBOL_INVALID')
            normalized = _normalized(row)
            key = tuple(normalized.get(field) for field in DIVIDEND_FIELDS)
            if key in seen:
                continue
            seen.add(key)
            yield row | dict(sourceId=f'raw.source_payload:{raw.id}:row:{i}')


def load_dividend_years(session, symbols, as_of: date, *, history_years=5):
    from app.portfolio.api.dividends import dividend_query_coverage
    symbols = tuple(symbols)
    coverage = dividend_query_coverage(session, symbols)
    originals = {}
    def read_originals(codes):
        try:
            originals.update(verified_dividend_rows(session, codes))
        except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
            if len(codes) > 1:
                for symbol in codes:
                    read_originals((symbol,))
            else:
                symbol = codes[0]
                query = coverage.get(symbol, {})
                coverage[symbol] = query | dict(querySource=None, issues=[*query.get('issues', []), str(exc)])
    # 沪深正常路径一次读取；北交所映射缺口只影响自身，不反复扫描完整沪深池。
    regular = tuple(s for s in symbols if not s.endswith('.BJ'))
    if regular:
        read_originals(regular)
    for symbol in symbols:
        if symbol.endswith('.BJ'):
            read_originals((symbol,))
    listings = dict(session.execute(select(Security.ts_code, Security.list_date).where(Security.ts_code.in_(symbols))).all())
    by_code = defaultdict(list)
    for r in session.scalars(select(Dividend).where(Dividend.ts_code.in_(symbols)).order_by(Dividend.id)):
        row = dividend_metadata(r)
        row.update({k: getattr(r, k) for k in ('ts_code', 'end_date', 'ann_date', 'div_proc',
            'record_date', 'ex_date', 'pay_date', 'cash_div_tax', 'cash_div')})
        by_code[r.ts_code].append(row | dict(sourceId=f'core.dividend:{r.id}'))
    for row in _sync_rows(session, symbols):
        by_code[row['ts_code']].append(row)
    result = {}
    for symbol in symbols:
        raw_id, rows = originals.get(symbol, (None, []))
        result[symbol] = dividend_years([*by_code[symbol], *rows], as_of, raw_id, history_years=history_years,
            query_coverage=coverage.get(symbol), listed_at=listings.get(symbol))
    return result
