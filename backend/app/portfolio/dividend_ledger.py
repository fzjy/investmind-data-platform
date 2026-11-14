"""同一只读Session中的供应商分配账本；不证明覆盖完整或账户财富。

只消费冻结control/unit绑定的HTTP原件。JSON标准浮点解码仅用于与已存
规范块比对；事实解析始终使用原字节的Decimal，避免先舍入再裁决修订。
"""
from __future__ import annotations

import base64
import binascii
from datetime import date, datetime, timezone
from decimal import Decimal, localcontext
from hashlib import sha256
import json
import math
import re

from app.db.models import SourcePayload, Security
from app.ingestion.repositories import content_hash
from app.portfolio.dividend_capture import _cursor, _valid_saved
from app.portfolio.dividend_contract import _date, resolve_distribution_evidence
from app.portfolio.dividend_report import DIVIDEND_FIELDS


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('HTTP正文含重复键')
        result[key] = value
    return result


def _constant(value):
    raise ValueError('HTTP正文含非有限数值')


def _original_rows(raw, fields, *, allow_more=False):
    payload = raw.payload
    http = payload['_http']
    if type(http.get('statusCode')) is not int or http['statusCode'] != 200 or http.get('credentialsRedacted') is not False:
        raise ValueError('HTTP状态或脱敏证据不合格')
    body = base64.b64decode(http['bodyBase64'], validate=True)
    if type(http.get('byteLength')) is not int or len(body) != http['byteLength'] or sha256(body).hexdigest() != http.get('bodySha256'):
        raise ValueError('HTTP字节摘要或长度不匹配')
    decoded = json.loads(body, object_pairs_hook=_object, parse_constant=_constant)
    exact = json.loads(body, parse_float=Decimal, object_pairs_hook=_object, parse_constant=_constant)
    if not isinstance(decoded, dict) or type(decoded.get('code')) is not int or decoded['code'] != 0:
        raise ValueError('HTTP业务状态不成功')
    data = decoded.get('data')
    if not isinstance(data, dict) or data.get('fields') != fields or not isinstance(data.get('items'), list):
        raise ValueError('HTTP字段合同不匹配')
    if 'has_more' in data and (type(data['has_more']) is not bool or (data['has_more'] and not allow_more)):
        raise ValueError('HTTP分页未完成或状态无效')
    # count是供应商信封元数据，未约定等于items行数，不能臆造该等式。
    # 仅消费已验证的事实块，historyComplete始终为False。
    # 序列化比对保留标量类型区别（False不可冒充0），同时允许正常JSON浮点规范化。
    normalized = {key:payload[key] for key in ('fields', 'items')}
    facts = {key:data[key] for key in ('fields', 'items')}
    if json.dumps(facts, sort_keys=True, allow_nan=False) != json.dumps(normalized, sort_keys=True, allow_nan=False):
        raise ValueError('HTTP正文与规范块不匹配')
    result = []
    for index, values in enumerate(exact['data']['items'], 1):
        if not isinstance(values, list) or len(values) != len(fields):
            raise ValueError('HTTP行宽不匹配')
        result.append(dict(zip(fields, values, strict=True)) |
                      {'sourceId':f'raw.source_payload:{raw.id}:row:{index}'})
    return result


def _number(value):
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 and (number != 0 or value == 0) else None


def _capital(item):
    total, bonus, conversion = (item[key] for key in ('stk_div', 'stk_bo_rate', 'stk_co_rate'))
    if total is None:
        return 'missing'
    if any(value is not None and value > total for value in (bonus, conversion)):
        return 'conflict'
    if bonus is not None and conversion is not None:
        with localcontext() as context:
            context.prec = max(len(v.as_tuple().digits) for v in (total, bonus, conversion)) + max(
                v.adjusted() for v in (total, bonus, conversion)) - min(v.as_tuple().exponent for v in (total, bonus, conversion)) + 4
            if bonus + conversion != total:
                return 'conflict'
    return 'reported-total' if _number(total) is not None else 'missing'


def _relevant(item, start, end):
    dates = []
    for key in ('record_date', 'ex_date', 'pay_date', 'div_listdate'):
        try:
            value = _date(item.get(key))
        except ValueError:
            return True
        if value:
            dates.append(value)
    # 未定位的实施腿不能证明在窗外；保守保留未知。
    return not dates or max(dates) >= start and min(dates) <= end


def _project(rows, raw, start, end, as_of):
    # 尚无实施事实的停止方案不影响实付；只让可能撤回实施记录的版本参与裁决。
    def period(row):
        try:
            return (_date(row.get('end_date')) or '')[:4]
        except ValueError:
            return ''
    def visible(row):
        try:
            return all(not (day := _date(row.get(key))) or day <= as_of.isoformat()
                       for key in ('ann_date', 'imp_ann_date'))
        except ValueError:
            return True  # 无效公开日期仍交裁决器标缺，不能静默视作未公开。
    implemented_years = {period(row) for row in rows if row.get('div_proc') in {'实施', '实施完成'} and visible(row)}
    retained = [row for row in rows if row.get('div_proc') not in
                {'取消','停止','停止实施','终止实施','取消实施','不实施'}
                or period(row) in implemented_years or not period(row) and implemented_years]
    resolved = resolve_distribution_evidence(retained, as_of, protect_paid=True)
    sources = {row['sourceId']:row for row in rows}
    events, gaps, issues = [], [], []
    zero_legs = {source for event in resolved['events']
                 if event['cash_div_tax'] == 0 and event['stk_div'] == 0 and _capital(event) == 'reported-total'
                 for source in event['sourceIds']}

    def gap(day, reason, source_ids, affects='both'):
        entry = dict(date=day, reason=reason, affects=affects, sourceIds=source_ids)
        if entry not in gaps:
            gaps.append(entry)
        if reason not in issues:
            issues.append(reason)

    for issue in resolved['issues']:
        code = issue['code']
        related = [sources[source] for source in issue['sourceIds'] if source in sources]
        if code in {'stage_not_implemented', 'future_date'}:
            continue
        if code == 'missing_event_identity' and all(source in zero_legs for source in issue['sourceIds']):
            continue
        if related and all(row.get('div_proc') not in {'实施','实施完成','取消','停止','停止实施','终止实施','取消实施','不实施'} for row in related):
            continue
        if code != 'unlocated_cancellation' and related and not any(_relevant(row, start, end) for row in related):
            continue
        gap(issue.get('date'), code + (':' + issue['field'] if 'field' in issue else ''), issue['sourceIds'])

    for item in resolved['events']:
        if not _relevant(item, start, end):
            continue
        reason = ['仅为已观察供应商分配证据；不证明完整覆盖或持仓资格']
        cash = _number(item['cash_div_tax'])
        capital_status = _capital(item)
        ratio = _number(item['stk_div']) if capital_status == 'reported-total' else None
        source_ids = item['sourceIds']
        anchor = item['record_date'] or item['ex_date']
        pay, listed = item['pay_date'], item['div_listdate']
        cash_status = ('not_required' if cash == 0 else 'missing' if cash is None or pay is None
                       else 'pending' if pay > end else 'paid')
        stock_status = ('pending' if ratio is None and listed is not None and listed > end
                        else 'missing' if ratio is None else 'not_required' if ratio == 0
                        else 'missing' if listed is None else 'pending' if listed > end else 'credited')
        if pay and pay > end:
            reason.append(f'现金待付，原支付日{pay}')
        if listed and listed > end:
            reason.append(f'红股待上市，原上市日{listed}')
        if item['imp_ann_date'] is None:
            reason.append('实施公告日期缺失；不证明完整实施证据')
        if cash_status == 'missing':
            gap(anchor, '现金金额或支付日缺失/数值无法安全表示', source_ids, 'cash')
        if capital_status != 'reported-total':
            gap(listed or anchor, '送转总率冲突' if capital_status == 'conflict' else '送转总率缺失/数值无法安全表示', source_ids, 'capital')
        if ratio is not None and ratio > 0 and listed is None:
            gap(anchor, '红股上市日期缺失', source_ids, 'capital')
        if anchor is None and (cash != 0 or ratio != 0):
            gap(None, '无法定位资格日', source_ids)
        identity = {key:item[key] for key in ('ts_code','end_date','record_date','ex_date')}
        if anchor is None:
            # 无稳定资格锚点时不能合并为同一事件；原件引用标识待定位事实。
            identity['sourceIds'] = source_ids
        originals = [sources[source]['_originalSource'] for source in source_ids]
        primary_source = originals[0]
        if any(datetime.fromisoformat(source['capturedAt']).date() > as_of for source in originals):
            reason.append('原件采集晚于基准日；不代表该历史时点的原始发布版本')
        events.append(dict(id='distribution:' + content_hash(identity), periodEnd=item['end_date'],
            announcedAt=item['ann_date'], implementationDate=item['imp_ann_date'],
            recordDate=item['record_date'] if item['record_date'] is None or item['record_date'] <= end else None,
            exDate=item['ex_date'] if item['ex_date'] is None or item['ex_date'] <= end else None,
            payDate=pay if pay is None or pay <= end else None,
            stockListedDate=listed if listed is None or listed <= end else None,
            cashPerShare=cash, stockRatio=ratio, cashStatus=cash_status, stockStatus=stock_status, capitalStatus=capital_status,
            sourceIds=source_ids, sourceHash=primary_source['sourceHash'], capturedAt=primary_source['capturedAt'],
            sourceOriginals=originals, reason='；'.join(reason)))
        if cash is not None:
            events[-1]['exactCash'] = str(item['cash_div_tax'])
        if _number(item['stk_div']) is not None:
            events[-1]['exactStockRatio'] = str(item['stk_div'])
    return events, gaps, issues


def _history_versions(rows, raw, start, end):
    """公开版本稀疏时间片；日期保留至整体end，由消费者按回看日判断生效。"""
    days = {start}
    for row in rows:
        try:
            announced = _date(row.get('ann_date'))
            implemented = _date(row.get('imp_ann_date'))
            known = max(announced, implemented or announced) if announced else None
            if known is not None and start < known <= end:
                days.add(known)
        except ValueError:
            # 无法定位公开日的问题仍由每个时间片的同一裁决器保守输出。
            continue
    result = []
    previous = None
    for day in sorted(days):
        events, gaps, issues = _project(rows, raw, start, end, date.fromisoformat(day))
        value = dict(events=events, gaps=gaps, issues=issues)
        if value != previous:
            result.append(dict(knownAt=day, **value))
            previous = value
    return result


def build_dividend_ledger(session, screener_base, symbols: tuple, as_of: date, price_starts: dict[str, str], *, evidence=None) -> dict:
    """不启事务、不写入、不联网；调用方负责同一发布事务及价格有效窗。

    symbols是调用方选定范围；不从当期screener.payload重新取池。父版本
    可以同日重建，sourceBaseVersion始终标采集血缘，不能改标当前父。
    """
    end = as_of.isoformat()
    result = dict(asOf=end, sourceBaseVersion=screener_base.version, captureIdentity='',
        basis='observed-supplier-events', historyComplete=False, sourceStatus='not_captured', assets={})
    for symbol in symbols:
        result['assets'][symbol] = dict(status='missing', reason='未捕获可验证原件',
            coverageStart=price_starts.get(symbol, end), coverageEnd=end, events=[], issues=[], gaps=[])

    def fail(status, reason):
        result['sourceStatus'] = status
        for asset in result['assets'].values():
            asset.update(status='missing', reason=reason, events=[], issues=[reason],
                gaps=[dict(date=None, reason=reason, affects='both', sourceIds=[])])
        return result

    with session.no_autoflush:
        control = _cursor(session, 'control') if evidence is None else evidence['control']
        if control is None:
            return result
        frozen = control.cursor_value if evidence is None else control
        if not isinstance(frozen, dict):
            return fail('invalid_evidence', '捕获control结构无效')
        result['sourceBaseVersion'] = frozen.get('baseVersion', '')
        result['captureIdentity'] = frozen.get('identity', '')
        if not symbols or len(set(symbols)) != len(symbols) or any(not isinstance(s, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', s) for s in symbols):
            return fail('base_changed', '输入证券范围无效')
        if frozen.get('asOf') != end or frozen.get('symbols') != sorted(symbols):
            return fail('base_changed', '捕获日期或证券范围与发布不一致')
        parent_day = screener_base.as_of
        parent_day = (parent_day.replace(tzinfo=timezone.utc) if parent_day.tzinfo is None else parent_day).astimezone(timezone.utc).date()
        if parent_day != as_of:
            return fail('base_changed', '发布父日期与账本不一致')
        keys = ('baseVersion', 'asOf', 'symbols', 'apiName', 'fields')
        if not isinstance(frozen.get('baseVersion'), str) or not frozen['baseVersion'] or frozen.get('apiName') != 'dividend' or frozen.get('fields') != list(DIVIDEND_FIELDS):
            return fail('invalid_evidence', '捕获字段或接口身份无效')
        if frozen.get('identity') != content_hash({key:frozen[key] for key in keys}):
            return fail('invalid_evidence', '捕获身份摘要无效')
        result['sourceStatus'] = 'observed'
        for symbol in symbols:
            asset = result['assets'][symbol]
            try:
                start = _date(price_starts.get(symbol))
                if start is None or start > end:
                    raise ValueError('价格窗口缺失或无效')
                unit_row = _cursor(session, f"{frozen['identity']}:unit:{symbol}") if evidence is None else evidence['units'].get(symbol)
                if unit_row is None:
                    if result['sourceStatus'] != 'invalid_evidence':
                        result['sourceStatus'] = 'not_captured'
                    continue
                unit = unit_row.cursor_value if evidence is None else unit_row
                if not isinstance(unit, dict) or unit.get('status') != 'completed' or not _valid_saved(session, unit, symbol, frozen['fields']):
                    raise ValueError('捕获unit或保存证据无效')
                if 'transportRawId' in unit and unit['transportRawId'] != unit['rawId']:
                    raise ValueError('捕获unit原始传输指针不匹配')
                raw = session.get(SourcePayload, unit['rawId'])
                from app.portfolio.evidence import verified_dividend_rows, _query_units, _latest_query, _query_end
                from app.portfolio.security_mapping import security_aliases
                rows = verified_dividend_rows(session, (symbol,)).get(symbol, (None, []))[1]
                events, gaps, issues = _project(rows, raw, start, end, as_of)
                aliases, mapping = security_aliases(session, (symbol,))
                queries = _query_units(session, aliases[symbol])
                query_ends, query_issues = [end], []
                for code in aliases[symbol]:
                    if not queries[code]:
                        query_issues.append('分红查询缺失：' + code)
                        continue
                    query_day, query_unit = _latest_query(queries[code])
                    if query_unit.get('status') != 'completed':
                        query_issues.append('分红查询未完成：' + code)
                        continue
                    query_raw = session.get(SourcePayload, query_unit['rawId'])
                    query_ends.append(_query_end(query_day, query_unit, query_raw))
                coverage_end = min(query_ends)
                listing = session.get(Security, symbol)
                if listing is None or listing.list_date is None:
                    query_issues.append('缺少上市日期，无法确定可统计历史')
                if coverage_end < start:
                    query_issues.append('查询截止日早于价格观察窗口')
                    coverage_end = end
                for reason in query_issues:
                    issues.append(reason)
                    gaps.append(dict(date=None, reason=reason, affects='both', sourceIds=[]))
                versions = _history_versions(rows, raw, start, end)
                for version in versions:
                    for reason in query_issues:
                        version['issues'].append(reason)
                        version['gaps'].append(dict(date=None, reason=reason, affects='both', sourceIds=[]))
                asset.update(status='observed', querySource=None if query_issues else f'raw.source_payload:{raw.id}',
                    reason='；'.join(query_issues) if query_issues else '已核对逐股查询原件；下列事件按观察窗口截取',
                    coverageStart=start, coverageEnd=coverage_end, events=events, gaps=gaps, issues=issues,
                    historyVersions=versions)
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError, binascii.Error):
                result['sourceStatus'] = 'invalid_evidence'
                reason = '捕获证据或价格窗口校验失败'
                asset.update(reason=reason, issues=[reason], gaps=[dict(date=None, reason=reason, affects='both', sourceIds=[])])
    return result
