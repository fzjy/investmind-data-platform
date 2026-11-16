from datetime import datetime

from sqlalchemy import inspect

from app.acquisition.contracts import StateKey
from app.acquisition.repository import StateRepository
from app.macro.audit_records import record


def read_compatible_macro_audit(factory, legacy_reader, *, now):
    report = legacy_reader()
    entries = report['entries']
    keys = {r['id']:StateKey(r['id'],'','series',r['id']) for r in entries if not r['id'].startswith('collection:')}
    with factory() as session:
        connection = session.connection()
        schema = 'ops'
        if not inspect(connection).has_table('data_acquisition_state',schema=schema):
            return report
        states = StateRepository(session).read(tuple(keys.values()))
    result = []
    for row in entries:
        evidence = states.get(keys.get(row['id']))
        checked = datetime.fromisoformat(row['checkedAt']) if row['checkedAt'] else None
        if (row['enabled'] and evidence and evidence.checked_at and evidence.checked_at <= now
            and evidence.status in ('success','failed') and evidence.scope.get('seriesId') == row['id']
            and evidence.scope.get('contract') == report['catalogVersion']
            and (not checked or evidence.checked_at > checked)):
            row = record(row['id'], True, evidence.status,evidence.checked_at,evidence.success_at,evidence.error_code,now=now)
        result.append(row)
    return {**report,'entries':result}
