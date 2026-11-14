"""确定性最近到期查询；指纹不是执行授权或持久预算。"""
from datetime import datetime, time, timedelta, timezone
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from app.macro.extensions.config import ExtensionConfig, ExtensionError, validate_group


NORMALIZER_VERSION = 'macro-extension-evidence-v1'
CONTRACTS = {
    'usdcnh': {'seriesId': 'fx.usdcnh', 'api': 'fx_daily', 'source': 'tushare',
        'sourceUrl': 'https://tushare.pro/document/2?doc_id=179',
        'unit': 'CNH/USD', 'frequency': 'daily', 'periodTimezone': 'UTC',
        'fields': ['ts_code', 'trade_date', 'exchange', 'bid_close', 'ask_close']},
    'nominalGdp': {'seriesId': 'cn.gdp.nominal_cumulative', 'api': 'cn_gdp', 'source': 'tushare',
        'sourceUrl': 'https://tushare.pro/document/2?doc_id=227',
        'unit': '亿元', 'frequency': 'quarterly', 'periodTimezone': 'Asia/Shanghai',
        'fields': ['quarter', 'gdp', 'gdp_yoy']},
    'vix': {'seriesId': 'us.volatility.vix', 'api': 'fred/series/observations', 'source': 'fred',
        'sourceUrl': 'https://fred.stlouisfed.org/series/VIXCLS', 'region': 'US',
        'unit': 'index', 'frequency': 'daily', 'periodTimezone': 'America/New_York',
        'useScope': 'personal-local-noncommercial',
        'fields': ['date', 'value', 'realtime_start', 'realtime_end']},
}


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value: object) -> str:
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def quarter_label(index: int) -> str:
    year, quarter = divmod(index, 4)
    if not 1 <= year <= 9999:
        raise ExtensionError('EXTENSION_PLAN_INVALID')
    return f'{year:04}Q{quarter + 1}'


def scheduled_due(schedule, now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ExtensionError('EXTENSION_PLAN_INVALID')
    local = now.astimezone(ZoneInfo(schedule.timezone))
    clock = time.fromisoformat(schedule.time)
    day = local.date()
    if schedule.frequency == 'weekly':
        day -= timedelta(days=(day.weekday() - schedule.weekday) % 7)
    due = datetime.combine(day, clock, local.tzinfo)
    if due.astimezone(timezone.utc) > local.astimezone(timezone.utc):
        day -= timedelta(days=7 if schedule.frequency == 'weekly' else 1)
        due = datetime.combine(day, clock, local.tzinfo)
    # 不把夏令时跳过的墙钟时刻当成真实到期点；重叠时刻固定fold=0。
    if due.astimezone(timezone.utc).astimezone(local.tzinfo).replace(tzinfo=None) != due.replace(tzinfo=None):
        raise ExtensionError('EXTENSION_PLAN_INVALID')
    return due


def _build(group_id: str, policy: dict, now: datetime) -> dict:
    group = validate_group(group_id, {**policy, 'collectionEnabled': False})
    from app.macro.extensions.config import OFFICIAL_MACRO_GROUPS
    if group_id in OFFICIAL_MACRO_GROUPS:
        from app.macro.official_macro_plan import build_official_macro_plan
        return build_official_macro_plan(group_id, policy, now)
    due = scheduled_due(group.schedule, now)
    if group_id == 'governmentBonds':
        from app.macro.fiscal_plan import build_government_plan
        return build_government_plan(group, policy, due)
    contract = CONTRACTS[group_id]
    if group_id == 'usdcnh':
        end = due.astimezone(timezone.utc).date() - timedelta(days=1)
        start = end - timedelta(days=group.lookbackPeriods - 1)
        start_period, end_period = start.isoformat(), end.isoformat()
        params = {'ts_code': 'USDCNH.FXCM', 'exchange': 'FXCM',
            'start_date': start.strftime('%Y%m%d'), 'end_date': end.strftime('%Y%m%d')}
    elif group_id == 'vix':
        vintage = due.astimezone(ZoneInfo('America/New_York')).date()
        end = vintage - timedelta(days=1)
        start = end - timedelta(days=group.lookbackPeriods - 1)
        start_period, end_period = start.isoformat(), end.isoformat()
        params = {'series_id': 'VIXCLS', 'observation_start': start_period, 'observation_end': end_period,
            'realtime_start': vintage.isoformat(), 'realtime_end': vintage.isoformat(), 'units': 'lin',
            'output_type': 1, 'file_type': 'json', 'order_by': 'observation_date', 'sort_order': 'asc',
            'offset': 0, 'limit': group.limits.maxResponseRows}
    else:
        end_index = due.year * 4 + (due.month - 1) // 3 - 1
        start_period = quarter_label(end_index - group.lookbackPeriods + 1)
        end_period = quarter_label(end_index)
        params = {'start_q': start_period, 'end_q': end_period}
    result = {'schemaVersion': 1, 'normalizerVersion': NORMALIZER_VERSION,
        'groupId': group_id, 'groupPolicy': policy,
        'groupPolicyHash': digest({'policy': policy, 'contract': contract, 'normalizer': NORMALIZER_VERSION}),
        'scheduledAt': due.isoformat(), 'startPeriod': start_period, 'endPeriod': end_period,
        **{key: contract[key] for key in ['seriesId', 'source', 'sourceUrl', 'unit', 'frequency', 'periodTimezone']},
        'query': {'api': contract['api'], 'params': params, 'fields': list(contract['fields'])},
        'limits': group.limits.model_dump()}
    if group_id == 'vix':
        result.update(region=contract['region'], useScope=contract['useScope'])
    return {**result, 'fingerprint': digest(result)}


def build_extension_plan(group_id: str, config: ExtensionConfig, now: datetime, *, calendar_rows=None) -> dict:
    try:
        checked = ExtensionConfig.model_validate(config.model_dump())
        policy = checked.groups[group_id].model_dump(exclude={'collectionEnabled'})
        from app.macro.exchange_summary_config import EXCHANGE_SUMMARY_GROUPS
        if group_id in EXCHANGE_SUMMARY_GROUPS:
            from app.macro.exchange_summary_plan import build_exchange_plan
            return build_exchange_plan(group_id,{**policy,'collectionEnabled':False},calendar_rows,now)
        return _build(group_id, policy, now)
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError, RecursionError):
        raise ExtensionError('EXTENSION_PLAN_INVALID') from None


def validate_extension_plan(plan: dict) -> None:
    """重建闭合合同；不接受仅给篡改的API重新算hash的假计划。"""
    try:
        if not isinstance(plan, dict) or 'collectionEnabled' in plan['groupPolicy']:
            raise ValueError()
        from app.macro.exchange_summary_config import EXCHANGE_SUMMARY_GROUPS
        if plan['groupId'] in EXCHANGE_SUMMARY_GROUPS:
            from app.macro.exchange_summary_plan import validate_exchange_plan
            validate_exchange_plan(plan)
            return
        expected = _build(plan['groupId'], plan['groupPolicy'], datetime.fromisoformat(plan['scheduledAt']))
        if canonical(expected) != canonical(plan):
            raise ValueError()
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError, RecursionError):
        raise ExtensionError('EXTENSION_PLAN_INVALID') from None
