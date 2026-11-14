"""累计→同年单季→上年同季名义同比，不替代真实GDP或充当平减率。"""
from decimal import Decimal, localcontext

from app.macro.extensions.config import ExtensionError
from app.macro.extensions.normalize import decimal_text, validate_evidence


def derive_nominal_gdp(evidence: dict) -> dict:
    validate_evidence(evidence)
    if evidence['plan']['groupId'] != 'nominalGdp':
        raise ExtensionError('EXTENSION_EVIDENCE_INVALID')
    rows = {row['period']: row for row in evidence['observations']}

    def single(label):
        row = rows.get(label)
        if row is None:
            return None, [], 'MISSING_SAME_QUARTER'
        refs = [row['sourceRef']]
        value = Decimal(row['value'])
        quarter = int(label[-1])
        if quarter > 1:
            previous = rows.get(f'{label[:4]}Q{quarter - 1}')
            if previous is None:
                return None, refs, 'MISSING_PREVIOUS_CUMULATIVE'
            refs.append(previous['sourceRef'])
            value -= Decimal(previous['value'])
        if value <= 0:
            return value, refs, 'NONPOSITIVE_SINGLE_QUARTER'
        return value, refs, None

    points = []
    with localcontext() as context:
        context.prec = 80
        for label in sorted(rows):
            amount, refs, error = single(label)
            prior_label = f'{int(label[:4]) - 1:04}Q{label[-1]}'
            prior, prior_refs, prior_error = single(prior_label)
            reason = error or prior_error
            yoy = None if reason else (amount / prior - 1) * 100
            points.append({'period': label,
                'amount': decimal_text(amount) if amount is not None else None,
                'nominalYoy': decimal_text(yoy) if yoy is not None else None,
                'status': 'unavailable' if reason else 'available', 'reason': reason,
                'sourceRefs': refs + prior_refs})
    return {'algorithmVersion': 'single-quarter-nominal-gdp-v1',
        'evidenceFingerprint': evidence['evidenceFingerprint'], 'unit': '亿元',
        'yoyUnit': '%', 'points': points, 'strictDeflatorComputed': False}
