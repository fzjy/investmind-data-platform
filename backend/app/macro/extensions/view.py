"""仅消费已完成Raw/Core的扩展观测；不采集，不改变固定20项目录。"""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import math
import re

from sqlalchemy import select

from app.db.models.core import MacroObservation
from app.db.models.ops import JobRun
from app.db.models.raw import IngestionBatch, SourcePayload
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.ingest import utc
from app.macro.extensions.normalize import decimal_text, number, quarter_date
from app.macro.extensions.plan import canonical, digest
from app.macro.extensions.store import ExtensionStore, JOB_NAME, RECOVERY_JOB_NAME, FAILURE_CODES, _job, instant


METADATA = {
    'usdcnh': {'id': 'fx.usdcnh', 'label': '离岸USD/CNH', 'unit': 'CNH/USD', 'frequency': 'daily'},
    'nominalGdp': {'id': 'cn.gdp.nominal_cumulative', 'label': '名义GDP累计值', 'unit': '亿元', 'frequency': 'quarterly'},
    'vix': {'id': 'us.volatility.vix', 'label': 'VIX', 'unit': 'index', 'frequency': 'daily'},
}
BASIS = 'retrieval-visible-restated-history-not-strict-pit'
VIX_ATTRIBUTION = 'CBOE via FRED. Copyright, 2016, Chicago Board Options Exchange, Inc. Reprinted with permission.'
REASONS = {None, 'NOT_COLLECTED', 'RESPONSE_EMPTY', 'CORE_PENDING', 'EXTENSION_ATTEMPT_UNCERTAIN', *FAILURE_CODES}


def _empty(group):
    return {**METADATA[group], 'status': 'unavailable', 'reason': 'NOT_COLLECTED',
        'executionState': 'not_collected', 'quoteSide': 'bid_close' if group == 'usdcnh' else None,
        'retrievedAt': None, 'evidenceFingerprint': None, 'points': []}


def load_extension_inputs(factory, as_of: datetime) -> dict:
    cutoff = instant(as_of)
    store = ExtensionStore(factory)
    sources = {key: _empty(key) for key in METADATA}
    with factory() as session:
        rows = session.scalars(select(JobRun).where(JobRun.job_name.in_((JOB_NAME, RECOVERY_JOB_NAME)),
            JobRun.started_at <= cutoff).order_by(JobRun.started_at, JobRun.id)).all()
        # 保留强引用，绑定校验不为每个点重复往返PG；依然逐点验证。
        retained = (
            session.scalars(select(SourcePayload).join(IngestionBatch).where(
                IngestionBatch.dataset.in_(tuple(f'macro-extension:{key}' for key in METADATA)))).all(),
            session.scalars(select(IngestionBatch).where(IngestionBatch.id.in_([row.id for row in rows]))).all(),
            session.scalars(select(MacroObservation).where(MacroObservation.series_id.in_(
                ('fx.usdcnh', 'cn.gdp.nominal_cumulative', 'us.volatility.vix')))).all(),
        )
        for row in rows:
            try:
                group = row.details['plan']['groupId']
                if group not in METADATA:
                    raise ValueError()
                details = _job(row, group)
                store._parent(session, row)
                store._root(session, details['plan'])
                # 后来才完成/失败的作业不能倒灌到历史截止。
                if row.finished_at is not None and utc(row.finished_at) > cutoff:
                    continue
                source = sources[group]
                phase = details['phase']
                source['executionState'] = phase
                source['reason'] = {'reserved': 'EXTENSION_ATTEMPT_UNCERTAIN', 'received': 'CORE_PENDING',
                    'failed': row.error_message, 'completed': None}[phase]
                if phase in ('received', 'completed'):
                    raw, retrieved = store._raw(session, row)
                    if retrieved > cutoff or phase != 'completed':
                        continue
                    evidence = raw['evidence']
                    source.update(retrievedAt=retrieved.isoformat(),
                        evidenceFingerprint=evidence['evidenceFingerprint'],
                        quoteSide=details['plan']['groupPolicy']['quoteSide'],
                        points=[{'period': p['period'], 'value': p['value'],
                            'reportedRealYoy': p['diagnostics'].get('reportedRealYoy'),
                            'sourceRow': p['sourceRef']['row']} for p in evidence['observations']])
                    source['status'] = 'available' if source['points'] else 'unavailable'
                    source['reason'] = None if source['points'] else 'RESPONSE_EMPTY'
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ExtensionError('EXTENSION_VIEW_SOURCE_INVALID') from None
        del retained
    from app.macro.fiscal_view import load_fiscal_inputs
    from app.macro.official_macro_view import load_official_macro_view
    official = load_official_macro_view(factory, cutoff)
    result = {'sources': sources, 'fiscal': load_fiscal_inputs(factory, cutoff)}
    if official is not None:
        result['official'] = official
    from app.macro.exchange_summary_view import load_exchange_summary_view
    market=load_exchange_summary_view(factory,cutoff)
    if market is not None:result['marketSummary']=market
    return result


def _finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError()
    return result if result else 0.0


def _real_points(points):
    result = []
    for point in points:
        label = point['month']
        parsed = date.fromisoformat(label)
        if parsed.isoformat() != label:
            raise ValueError()
        # 旧目录仍保持原样；非季度标签不猜所属季度，也不能阻断全无扩展的旧快照。
        if parsed.day != 1 or parsed.month not in (1, 4, 7, 10):
            continue
        result.append({'period': f'{parsed.year:04}Q{(parsed.month - 1) // 3 + 1}',
            'value': decimal_text(number(point['value']))})
    if len({p['period'] for p in result}) != len(result):
        raise ValueError()
    return sorted(result, key=lambda p: p['period'])


def _calculate(sources, real, fiscal=None, official=None, market_summary=None):
    fx_points = sources['usdcnh']['points']
    fx = {'value': None, 'changePercent': None, 'period': None}
    if fx_points:
        last = fx_points[-1]
        fx = {'value': _finite(Decimal(last['value'])), 'period': last['period'], 'changePercent': None}
        if len(fx_points) >= 2:
            fx['changePercent'] = _finite((Decimal(last['value']) / Decimal(fx_points[-2]['value']) - 1) * 100)
    rows = {p['period']: p for p in sources['nominalGdp']['points']}
    actual = {p['period']: Decimal(p['value']) for p in real}

    def single(label):
        row = rows.get(label)
        if row is None:
            return None, 'MISSING_SAME_QUARTER'
        amount = Decimal(row['value'])
        if label[-1] != '1':
            previous = rows.get(f'{label[:4]}Q{int(label[-1]) - 1}')
            if previous is None:
                return None, 'MISSING_PREVIOUS_CUMULATIVE'
            amount -= Decimal(previous['value'])
        return amount, None if amount > 0 else 'NONPOSITIVE_SINGLE_QUARTER'

    points = []
    for label, row in sorted(rows.items()):
        amount, error = single(label)
        base, base_error = single(f'{int(label[:4]) - 1:04}Q{label[-1]}')
        reason = error or base_error
        nominal = None if reason else (amount / base - 1) * 100
        real_value = actual.get(label)
        approximate = strict = None
        if reason is None:
            if real_value is None:
                reason = 'REAL_GDP_MISSING'
            elif real_value.quantize(Decimal('0.0000000001'), rounding=ROUND_HALF_EVEN) != Decimal(row['reportedRealYoy']).quantize(Decimal('0.0000000001'), rounding=ROUND_HALF_EVEN):
                reason = 'REAL_GDP_REVISION_MISMATCH'
            elif real_value <= -100:
                reason = 'REAL_GDP_BASE_NONPOSITIVE'
            else:
                approximate = nominal - real_value
                strict = ((1 + nominal / 100) / (1 + real_value / 100) - 1) * 100
        points.append({'period': label, 'month': quarter_date(label).isoformat(),
            'amount': None if amount is None else _finite(amount),
            'nominalYoy': None if nominal is None else _finite(nominal),
            'realYoy': None if real_value is None else _finite(real_value),
            'approxDeflator': None if approximate is None else _finite(approximate),
            'strictDeflator': None if strict is None else _finite(strict), 'reason': reason})
    result = {'schemaVersion': '1.0', 'algorithmVersion': 'macro-extension-view-v1', 'basis': BASIS,
        'sourceHash': digest({'sources': sources, 'realGdp': real}), 'sources': deepcopy(sources),
        'realGdp': real, 'fx': fx, 'nominalGdp': points, 'deflatorUnit': '百分点',
        'deflatorFormula': '名义GDP单季同比−实际GDP同比（近似差）；严格比率另列，非价格指数水平'}
    if 'vix' in sources:
        vix = sources['vix']['points']
        result['schemaVersion'] = '1.1'
        result['vix'] = {'value': _finite(Decimal(vix[-1]['value'])) if vix else None,
            'changePercent': _finite((Decimal(vix[-1]['value']) / Decimal(vix[-2]['value']) - 1) * 100) if len(vix) >= 2 else None,
            'period': vix[-1]['period'] if vix else None,
            'sourceUrl': 'https://fred.stlouisfed.org/series/VIXCLS', 'attribution': VIX_ATTRIBUTION,
            'useScope': 'personal-local-noncommercial'}
    if fiscal is not None:
        from app.macro.fiscal_view import fiscal_view_valid
        if 'vix' not in sources or not fiscal_view_valid(fiscal):
            raise ValueError()
        result.update(schemaVersion='1.2', fiscal=deepcopy(fiscal),
            sourceHash=digest({'sources': sources, 'realGdp': real, 'fiscal': fiscal}))
    if official is not None:
        from app.macro.official_macro_view import official_macro_view_valid
        if 'vix' not in sources or not official_macro_view_valid(official):
            raise ValueError()
        result.update(schemaVersion='1.3', official=deepcopy(official), fiscal=deepcopy(fiscal),
            sourceHash=digest({'sourceEvidenceHash': digest({'sources': sources, 'realGdp': real}),
                'fiscalEvidenceHash': digest({k:fiscal[k] for k in ('window','documents','latestAttempt')}) if fiscal is not None else None,
                'officialEvidenceHash': official['evidenceHash']}))
    if market_summary is not None:
        from app.macro.exchange_summary_view import exchange_summary_view_valid
        if 'vix' not in sources or not exchange_summary_view_valid(market_summary):raise ValueError()
        result.update(schemaVersion='1.4',marketSummary=deepcopy(market_summary),fiscal=deepcopy(fiscal),official=deepcopy(official),
            sourceHash=digest({'sourceEvidenceHash':digest({'sources':sources,'realGdp':real}),
                'fiscalEvidenceHash':digest({k:fiscal[k] for k in ('window','documents','latestAttempt')}) if fiscal is not None else None,
                'officialEvidenceHash':official['evidenceHash'] if official is not None else None,
                'marketEvidenceHash':market_summary['evidenceHash']}))
    return result


def build_extension_view(inputs: dict, real_gdp_points=()) -> dict:
    with localcontext() as context:
        context.prec = 80
        return _calculate(inputs['sources'], _real_points(real_gdp_points), inputs.get('fiscal'), inputs.get('official'), inputs.get('marketSummary'))


def _sources_valid(sources):
    if type(sources) is not dict or set(sources) not in ({'usdcnh', 'nominalGdp'}, set(METADATA)):
        return False
    for group, source in sources.items():
        if type(source) is not dict or set(source) != set(_empty(group)):
            return False
        if any(source[key] != value for key, value in METADATA[group].items()):
            return False
        if (source['status'] not in ('available', 'unavailable') or source['reason'] not in REASONS
            or source['executionState'] not in ('not_collected', 'reserved', 'received', 'failed', 'completed')
            or (source['quoteSide'] not in ('bid_close', 'ask_close') if group == 'usdcnh' else source['quoteSide'] is not None)
            or type(source['points']) is not list or len(source['points']) > (64 if group == 'nominalGdp' else 366)):
            return False
        if source['retrievedAt'] is None:
            if source['points'] or source['evidenceFingerprint'] is not None or source['status'] != 'unavailable':
                return False
        else:
            retrieved = instant(datetime.fromisoformat(source['retrievedAt']))
            if retrieved.isoformat() != source['retrievedAt'] or not re.fullmatch('[0-9a-f]{64}', source['evidenceFingerprint']):
                return False
            if (source['status'] == 'available') != bool(source['points']):
                return False
        labels, indices = [], set()
        for point in source['points']:
            if type(point) is not dict or set(point) != {'period', 'value', 'reportedRealYoy', 'sourceRow'}:
                return False
            label = point['period']
            if group != 'nominalGdp':
                if date.fromisoformat(label).isoformat() != label or point['reportedRealYoy'] is not None:
                    return False
            else:
                quarter_date(label)
                if type(point['reportedRealYoy']) is not str or decimal_text(number(point['reportedRealYoy'])) != point['reportedRealYoy']:
                    return False
            if (type(point['value']) is not str or decimal_text(number(point['value'])) != point['value']
                or number(point['value']) <= 0 or type(point['sourceRow']) is not int
                or not 0 <= point['sourceRow'] < (64 if group == 'nominalGdp' else 366) or point['sourceRow'] in indices):
                return False
            indices.add(point['sourceRow'])
            labels.append(label)
        if labels != sorted(set(labels)):
            return False
    return True


def extension_view_valid(value: object, expected: dict | None = None) -> bool:
    try:
        if type(value) is not dict or not _sources_valid(value['sources']) or type(value['realGdp']) is not list:
            return False
        real = value['realGdp']
        for p in real:
            if set(p) != {'period', 'value'} or type(p['value']) is not str or decimal_text(number(p['value'])) != p['value']:
                return False
            quarter_date(p['period'])
        if [p['period'] for p in real] != sorted({p['period'] for p in real}):
            return False
        with localcontext() as context:
            context.prec = 80
            rebuilt = _calculate(value['sources'], real, value.get('fiscal'), value.get('official'), value.get('marketSummary'))
        return canonical(value) == canonical(rebuilt) and (expected is None or canonical(value) == canonical(expected))
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, OverflowError):
        return False
