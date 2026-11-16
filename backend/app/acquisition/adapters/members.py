from sqlalchemy import select
from app.acquisition.contracts import StateKey
from app.acquisition.tracking import record_terminal
from app.db.models import JobRun


def _target(plan, doc):
    return (plan.get('target') or {}).get('code') or doc.get('fundCode') or doc.get('indexCode')


def record_member_plan(session, row, checked):
    plan, doc = row.details['plan'],row.details['document']
    code = _target(plan,doc)
    if not code:
        return
    from app.acquisition.catalog import load_bindings
    from pathlib import Path
    ROOT = Path(__file__).resolve().parents[4]
    target = next((b.item_id for b in load_bindings(ROOT) if b.entry_id=='macro-enterprise'
                   and code in (b.item_id,b.item_id.split('.')[0])),None)
    if not target:
        return
    key = StateKey('macro-enterprise',target,'product','macro-transmission')
    scope = {'plan':plan['fingerprint'],'target':code}
    expected = [d for d in plan.get('policy',{}).get('documents',[]) if _target(plan,d)==code]
    if not expected:
        expected = [doc]
    related = session.scalars(select(JobRun).where(JobRun.job_name == row.job_name,
                   JobRun.details['plan']['fingerprint'].as_string() == plan['fingerprint'])).all()
    slots = {r.details['document'].get('id'):r for r in related if _target(plan,r.details['document'])==code}
    required = [slots.get(d.get('id')) for d in expected]
    failure = next((r for r in required if r and r.status=='failed'),None)
    complete = all(r and r.details.get('phase')=='completed' for r in required)
    count = sum(r.row_count or 0 for r in required if r)
    status = 'failed' if failure else 'success' if complete and count>0 else 'unrecorded'
    record_terminal(session,key,started_at=checked,checked_at=checked,status=status,scope=scope,
                    code='MEMBER_SOURCE_FAILED' if failure else None,
                    references=tuple({'runId':r.id} for r in required if r)[:8],
                    reason='目标成分及文件范围已核验' if status=='success' else '目标文件未核全或获取失败')
