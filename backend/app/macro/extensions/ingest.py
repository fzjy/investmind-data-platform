"""处理闭合扩展序列；事务由调用者持有，Raw保留精确值。"""
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN, localcontext

from sqlalchemy import select

from app.db.models.core import MacroObservation
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.normalize import quarter_date, validate_evidence
from app.macro.extensions.plan import digest


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def storage_value(value: str) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        number = Decimal(value).quantize(Decimal('0.0000000001'), rounding=ROUND_HALF_EVEN)
    if not number.is_finite() or not 0 < number < Decimal('1e18'):
        raise ExtensionError('EXTENSION_CORE_VALUE_UNSUPPORTED')
    return number


def observed_date(plan: dict, period: str) -> date:
    return quarter_date(period) if plan['groupId'] == 'nominalGdp' else date.fromisoformat(period)


def validate_binding(session, plan: dict, observation: dict, binding: dict, retrieved_at: datetime) -> None:
    if (type(binding) is not dict or set(binding) != {'coreId', 'period', 'value', 'revision', 'rawRow'}
        or type(binding['coreId']) is not int or type(binding['rawRow']) is not int
        or binding['period'] != observation['period'] or binding['rawRow'] != observation['sourceRef']['row']):
        raise ExtensionError('EXTENSION_EXECUTION_INVALID')
    row = session.get(MacroObservation, binding['coreId'])
    expected = storage_value(observation['value'])
    if (row is None or row.source != plan['source'] or row.series_id != plan['seriesId']
        or row.frequency != plan['frequency'] or row.unit != plan['unit']
        or row.source_url != plan['sourceUrl'] or row.region != plan.get('region', 'CN') or row.seasonal_adjustment != 'NSA'
        or row.observed_at != observed_date(plan, observation['period']) or row.value != expected
        or binding['value'] != str(expected) or row.revision != binding['revision']
        or utc(row.released_at) > utc(retrieved_at)
        or (row.valid_until is not None and utc(row.valid_until) <= utc(retrieved_at))):
        raise ExtensionError('EXTENSION_EXECUTION_INVALID')


def ingest_evidence(session, evidence: dict, retrieved_at: datetime) -> list[dict]:
    validate_evidence(evidence)
    plan = evidence['plan']
    result = []
    for observation in evidence['observations']:
        value = storage_value(observation['value'])
        period = observed_date(plan, observation['period'])
        existing = session.scalars(select(MacroObservation).where(
            MacroObservation.series_id == plan['seriesId'],
            MacroObservation.observed_at == period).with_for_update()).all()
        if any(row.source != plan['source'] for row in existing):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        if any(utc(row.released_at) > utc(retrieved_at) for row in existing):
            raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
        current = [row for row in existing if row.valid_until is None]
        if len(current) > 1 or (existing and not current):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        row = current[0] if current else None
        if row is not None and (row.frequency != plan['frequency'] or row.unit != plan['unit']
            or row.source_url != plan['sourceUrl'] or row.region != plan.get('region', 'CN') or row.seasonal_adjustment != 'NSA'):
            raise ExtensionError('EXTENSION_EXECUTION_INVALID')
        if row is None or row.value != value:
            if row is not None:
                if utc(row.released_at) >= utc(retrieved_at):
                    raise ExtensionError('EXTENSION_CLOCK_REGRESSION')
                row.valid_until = retrieved_at
            row = MacroObservation(source=plan['source'], series_id=plan['seriesId'], name=plan['seriesId'],
                region=plan.get('region', 'CN'), frequency=plan['frequency'], unit=plan['unit'], seasonal_adjustment='NSA',
                observed_at=period, released_at=retrieved_at,
                revision=digest({'evidence': evidence['evidenceFingerprint'], 'period': observation['period']}),
                value=value, source_url=plan['sourceUrl'])
            session.add(row)
            session.flush()
        result.append({'coreId': row.id, 'period': observation['period'], 'value': str(value),
            'revision': row.revision, 'rawRow': observation['sourceRef']['row']})
    return result
