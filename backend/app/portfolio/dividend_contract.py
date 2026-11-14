"""分红纯事实合同；不证明历史覆盖、持仓资格或整窗股本不变。

身份使用证券、报告期及资格日（缺失时用除权日），支付日仅为修订内容。
资格日和除权日全部变更时，没有稳定事件 ID 无法断言属于同一事件。
同日冲突及无法定位的停止记录保守阻断。sourceIds 仅表示最终版本的
证据；未提供 sourceId 时以输入内容指纹生成稳定身份，不使用行号。

status: ok=有事实且无缺口，partial=有事实及缺口，blocked=仅有缺口，
empty=无可计事实且无缺口（包括空输入或已撤销事件）。资本 missing
不阻断已明确的现金，其他坏字段阻断该条。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import math
import re


_IMPLEMENTED = frozenset({'实施', '实施完成'})
_CANCELLED = frozenset({'取消', '停止', '停止实施', '终止实施', '取消实施', '不实施'})
_ANCHORS = ('record_date', 'ex_date', 'pay_date')
_DATES = ('end_date', 'ann_date', *_ANCHORS, 'imp_ann_date', 'base_date')
_CAPITAL = ('stk_div', 'stk_bo_rate', 'stk_co_rate')
_NUMBERS = ('cash_div_tax', 'cash_div', *_CAPITAL, 'base_share')


def _missing(value: object) -> bool:
    return value is None or isinstance(value, str) and not value.strip()


def _date(value: object) -> str | None:
    if _missing(value):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        value = value.strip()
        if re.fullmatch(r'\d{8}', value):
            return date(int(value[:4]), int(value[4:6]), int(value[6:])).isoformat()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return date.fromisoformat(value).isoformat()
    raise ValueError('日期须为 date、ISO 或 YYYYMMDD')


def _number(value: object) -> Decimal | None:
    if _missing(value):
        return None
    if isinstance(value, bool):
        raise ValueError('布尔值不是金额或资本数值')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError('数值须有限且非负')
        return number
    except InvalidOperation as exc:
        raise ValueError('无法解析数值') from exc


def _canonical(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _source(row: Mapping) -> str:
    identity = row.get('sourceId')
    if not _missing(identity):
        return str(identity)
    content = {key: value for key, value in row.items() if key != 'sourceId'}
    payload = json.dumps(_canonical(content), sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    return 'content:' + sha256(payload.encode('utf-8')).hexdigest()


def _related(left: dict, right: dict) -> bool:
    if left['end_date'] != right['end_date']:
        return False
    if left['record_date'] is not None and right['record_date'] is not None:
        return left['record_date'] == right['record_date']
    return left['ex_date'] is not None and left['ex_date'] == right['ex_date']


def _resolve_dividend_events(rows: Sequence[Mapping], as_of: date, *, legacy_cash=False, evidence=False,
                            protect_paid=False) -> dict:
    """按截止日解决实施事件；无环境、数据库、网络或输入修改。

    同一证券报告期内用资格日优先定位修订；相同公告日的现金、日期或资本
    证据不同不任意择一。预案不参加实施修订，停止阶段参加。缺少日期
    的实施记录不具备现金资格；停止记录只有唯一匹配时才可局部撤销。
    """
    cutoff = as_of.isoformat()
    cash_field = 'cash_div' if legacy_cash else 'cash_div_tax'
    dates = (*_DATES, 'div_listdate') if evidence else _DATES
    issues: list[dict] = []
    buckets: dict[tuple[str, str], list[dict]] = {}

    def issue(code: str, item: dict, field: str | None = None) -> None:
        entry = dict(code=code, ts_code=item.get('ts_code'), end_date=item.get('end_date'),
                     ann_date=item.get('ann_date'), sourceIds=sorted(item['sourceIds']))
        if field is not None:
            entry['field'] = field
        if evidence:
            entry['date'] = next((item.get(key) for key in ('record_date', 'ex_date', 'pay_date', 'div_listdate')
                                  if item.get(key) is not None), None)
        issues.append(entry)

    for row in rows:
        data = row.get('data')
        raw = dict(data) if isinstance(data, Mapping) else {}
        raw.update({key: value for key, value in row.items() if key != 'data'})
        item: dict = dict(sourceIds=[_source(row)], invalid=False)
        symbol = raw.get('ts_code')
        item['ts_code'] = symbol.strip() if isinstance(symbol, str) else None
        if not item['ts_code']:
            issue('missing_symbol', item, 'ts_code')
            item['invalid'] = True
        for field in dates:
            try:
                item[field] = _date(raw.get(field))
            except ValueError:
                item[field] = None
                item['invalid'] = True
                issue('invalid_date', item, field)
        for field in _NUMBERS:
            try:
                item[field] = _number(raw.get(field))
            except (ValueError, TypeError, OverflowError):
                item[field] = None
                item['invalid'] = True
                issue('invalid_number', item, field)
        for field in ('ann_date', 'end_date'):
            if item[field] is None:
                issue('missing_' + field, item, field)
                item['invalid'] = True
        stage = raw.get('div_proc')
        item['stage'] = stage
        if not isinstance(stage, str) or stage not in _IMPLEMENTED and stage not in _CANCELLED:
            issue('stage_not_implemented', item, 'div_proc')
            continue
        # 公告或实施公告尚不可知的版本，不应覆盖截止日已经知道的版本。
        future = [field for field in ('ann_date', 'imp_ann_date')
                  if item[field] is not None and item[field] > cutoff]
        if future:
            for field in future:
                issue('future_date', item, field)
            continue
        if not item['ts_code'] or not item['end_date']:
            continue
        key = (item['ts_code'], item['end_date'][:4])
        buckets.setdefault(key, []).append(item)

    events: list[dict] = []
    for items in buckets.values():
        implemented = [item for item in items if item['stage'] in _IMPLEMENTED]
        groups: list[list[dict]] = []
        blocked = False
        # 先建立已知资格日的事件，缺资格日的除权锚点只能唯一匹配，不能
        # 通过一条不完整记录把两个资格日不同的事件桥接合并。
        for item in sorted(implemented, key=lambda item: item['record_date'] is None):
            matching = [group for group in groups if any(_related(item, old) for old in group)]
            if len(matching) > 1:
                issue('ambiguous_event_identity', item)
                blocked = True
            elif matching:
                matching[0].append(item)
            else:
                groups.append([item])
        for cancel in (item for item in items if item['stage'] in _CANCELLED):
            matching = [group for group in groups if any(_related(cancel, old) for old in group)]
            if len(matching) != 1 or cancel['invalid'] or cancel['ann_date'] is None:
                issue('unlocated_cancellation', cancel)
                blocked = True
            else:
                matching[0].append(cancel)
        if blocked:
            continue
        for group in groups:
            # 缺公告日无法断言版本先后，不能回退到看似有效的旧记录。
            if any(item['ann_date'] is None for item in group):
                continue
            latest_date = max(item['ann_date'] for item in group)
            latest = [item for item in group if item['ann_date'] == latest_date]
            sources = sorted({source for item in latest for source in item['sourceIds']})
            winner = dict(latest[0], sourceIds=sources)
            if protect_paid:
                # 更正生效前，旧版记载的正现金已经到支付日，不能静默重写到账事实。
                version_dates = sorted({v['ann_date'] for v in group})
                valid_until = {day: min(max(v['ann_date'], v['imp_ann_date'] or '') for v in group
                                        if v['ann_date'] == following)
                               for day, following in zip(version_dates, version_dates[1:])}
                paid = [v for v in group if v['stage'] in _IMPLEMENTED and not v['invalid']
                        and v['ann_date'] < latest_date and v['pay_date'] and v['pay_date'] <= valid_until[v['ann_date']]
                        and v['cash_div_tax'] is not None and v['cash_div_tax'] > 0
                        and (winner['stage'] in _CANCELLED or
                             (v['cash_div_tax'], v['pay_date']) != (winner['cash_div_tax'], winner['pay_date']))]
                if paid:
                    refs = sorted(set(sources).union(*(v['sourceIds'] for v in paid)))
                    issue('paid_revision_conflict', dict(winner, sourceIds=refs))
                    continue
            latest_conflict = False
            for announcement in sorted({item['ann_date'] for item in group}):
                versions = [item for item in group if item['ann_date'] == announcement]
                signatures = {tuple(item[field] for field in (*dates, *_NUMBERS)) +
                              (item['stage'] in _CANCELLED,) for item in versions}
                if len(signatures) > 1:
                    # 仅用于诊断上下文的确定性排序；冲突事实绝不择一输出。
                    context = min(versions, key=lambda item: json.dumps(_canonical(item), sort_keys=True))
                    context = dict(context, sourceIds=sorted({source for item in versions
                                                             for source in item['sourceIds']}))
                    issue('same_day_conflict', context)
                    latest_conflict |= announcement == latest_date
            if latest_conflict:
                continue
            if any(item['invalid'] for item in latest) or winner['stage'] in _CANCELLED:
                continue
            if evidence:
                # 证据模式共用版本裁决，腿的缺失/未来由读模型明确表达。
                # 不要求现金支付字段才能保留独立送转腿。
                invalid_order = False
                for left, right in (('record_date', 'ex_date'), ('ex_date', 'pay_date'),
                                    ('record_date', 'pay_date'), ('record_date', 'div_listdate'),
                                    ('ex_date', 'div_listdate')):
                    if winner[left] is not None and winner[right] is not None and winner[left] > winner[right]:
                        issue('invalid_date_order', winner, f'{left}/{right}')
                        invalid_order = True
                if invalid_order:
                    continue
                if winner['record_date'] is None and winner['ex_date'] is None:
                    issue('missing_event_identity', winner)
                events.append({field: winner[field] for field in ('ts_code', *dates, *_NUMBERS)} |
                              dict(sourceIds=sources))
                continue
            eligible = True
            if legacy_cash and winner['pay_date'] is None and winner['ex_date'] is None:
                issue('missing_effective_cash_date', winner)
                eligible = False
            required = (cash_field,) if legacy_cash else ('imp_ann_date', 'pay_date', cash_field)
            for field in required:
                if winner[field] is None:
                    issue('missing_' + field, winner, field)
                    eligible = False
            if winner['record_date'] is None and winner['ex_date'] is None:
                issue('missing_event_identity', winner)
                eligible = False
            for field in _ANCHORS:
                if winner[field] is not None and winner[field] > cutoff:
                    issue('future_date', winner, field)
                    eligible = False
            for left, right in (('record_date', 'ex_date'), ('ex_date', 'pay_date'), ('record_date', 'pay_date')):
                if winner[left] is not None and winner[right] is not None and winner[left] > winner[right]:
                    issue('invalid_date_order', winner, f'{left}/{right}')
                    eligible = False
            if not eligible:
                continue
            cash = float(winner[cash_field])
            if not math.isfinite(cash) or cash == 0 and winner[cash_field] != 0:
                issue('unrepresentable_cash', winner, cash_field)
                continue
            capital = [winner[field] for field in _CAPITAL]
            if any(value is not None and value > 0 for value in capital):
                capital_status = 'share-changing'
            elif all(value == 0 for value in capital):
                capital_status = 'unchanged-distribution'
            else:
                capital_status = 'missing'
                issue('missing_capital', winner)
            event = {field: winner[field] for field in ('ts_code', 'end_date', 'ann_date',
                     *_ANCHORS, 'imp_ann_date')}
            event[cash_field] = cash
            event.update(sourceIds=sources, capitalStatus=capital_status)
            events.append(event)

    events.sort(key=lambda event: tuple(event[field] or '' for field in ('ts_code', 'end_date', *_ANCHORS)))
    # 去重并排序，保证输入顺序不影响诊断或结果。
    unique = {json.dumps(entry, sort_keys=True, ensure_ascii=True): entry for entry in issues}
    issues = [unique[key] for key in sorted(unique)]
    status = ('partial' if events else 'blocked') if issues else ('ok' if events else 'empty')
    return dict(events=events, issues=issues, status=status)


def resolve_dividend_events(rows: Sequence[Mapping], as_of: date) -> dict:
    """严格税前现金事实；无实施公告/支付证据不宣称可用。"""
    return _resolve_dividend_events(rows, as_of)


def resolve_distribution_evidence(rows: Sequence[Mapping], as_of: date, *, protect_paid=False) -> dict:
    """共享修订、取消和时点裁决的分配证据；Decimal及未来到账日期保持原值。"""
    return _resolve_dividend_events(rows, as_of, evidence=True, protect_paid=protect_paid)


def select_legacy_dividend_rows(rows, as_of: date):
    """旧派生口径保留cash_div及未知实施日；只共享修订规则，不冒充税前证据。"""
    sources = {f'core.dividend:{row.id}': row for row in rows}
    normalized = []
    for source, row in sources.items():
        data = dict(row.data or {})
        for field in ('imp_ann_date', 'base_date'):
            if type(data.get(field)) is int and len(str(data[field])) == 8:
                data[field] = str(data[field])
        normalized.append({**data, **{key: getattr(row, key) for key in
            ('ts_code', 'end_date', 'ann_date', 'div_proc', 'record_date', 'ex_date', 'pay_date', 'cash_div', 'cash_div_tax')},
            'sourceId': source})
    resolved = _resolve_dividend_events(normalized, as_of, legacy_cash=True)
    return [sources[sorted(event['sourceIds'])[-1]] for event in resolved['events']]
