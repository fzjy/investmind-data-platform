from datetime import UTC, datetime
from app.acquisition.macro_compat import read_compatible_macro_audit
from app.macro.audit_records import record

def test_empty_state_preserves_full_contract(factory):
    now = datetime.now(UTC)
    doc = {'schemaVersion':'1.0','basis':'current-collection-records','readAt':now.isoformat(),
           'catalogVersion':'test','entries':[record('cn.cpi.yoy',True,'success',now,now)]}
    assert read_compatible_macro_audit(factory, lambda:doc, now=now) == doc
