"""独立官方宏观的闭合周计划；静态文档或一层目录派生，不执行 IO。"""
from datetime import datetime

from app.macro.extensions.config import OFFICIAL_MACRO_GROUPS, validate_group
from app.macro.extensions.plan import digest, scheduled_due

NORMALIZER = 'official-housing-fiscal-v1'


def build_official_macro_plan(group_id: str, policy: dict, now: datetime) -> dict:
    if group_id not in OFFICIAL_MACRO_GROUPS:
        raise ValueError('OFFICIAL_MACRO_PLAN_INVALID')
    checked = validate_group(group_id, {**policy, 'collectionEnabled': False})
    due = scheduled_due(checked.schedule, now)
    last_month = due.year * 12 + due.month - 2
    first_month = last_month - checked.lookbackPeriods + 1
    month = lambda index: f'{index // 12:04}-{index % 12 + 1:02}-01'
    end, start = month(last_month), month(first_month)
    quarter_index = due.year * 4 + (due.month - 1) // 3 - 1
    documents = [doc.model_dump() for doc in checked.documents]
    for doc in documents:
        if doc['role'] in ('household-directory','property-directory','special-directory','special-budget-directory','tier1-directory','tier1-directory-page') or doc['derivedFrom'] is not None:
            continue
        if doc['role'] == 'special-quota':
            if int(doc['period']) > due.year:
                raise ValueError('OFFICIAL_MACRO_FUTURE_DOCUMENT')
        elif doc['role'] not in ('property-report', 'property-directory'):
            if not start <= doc['period']+'-01' <= end:
                raise ValueError('OFFICIAL_MACRO_FUTURE_DOCUMENT')
    if any(d['role']=='special-directory' for d in documents) and not any(d['role']=='special-budget-directory' for d in documents):
        quota=next(d for d in documents if d['role']=='special-quota')
        if quota['period']!=end[:4]:raise ValueError('OFFICIAL_SPECIAL_TARGET_QUOTA_REQUIRED')
    frozen = checked.model_dump(exclude={'collectionEnabled'})
    value = {'schemaVersion': 1, 'normalizerVersion': NORMALIZER,
             'groupId': group_id, 'groupPolicy': frozen,
             'groupPolicyHash': digest({'groupId': group_id, 'policy': frozen, 'normalizer': NORMALIZER}),
             'scheduledAt': due.isoformat(), 'startPeriod': start, 'endPeriod': end,
             'latestQuarter': f'{quarter_index // 4}-Q{quarter_index % 4 + 1}',
             'documents': documents, 'limits': checked.limits.model_dump()}
    return {**value, 'fingerprint': digest(value)}
