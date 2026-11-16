from sqlalchemy import select
from app.acquisition.contracts import Availability
from app.db.models import SourcePayload
from app.ingestion.repositories import content_hash


def raw_availability(session,key,proof):
    """只检查该对象最近成功凭证所引用的原件。"""
    batch_ids = [r['batchId'] for r in proof.references if r.get('batchId')]
    if not batch_ids:
        return None
    for raw in session.scalars(select(SourcePayload).where(SourcePayload.batch_id.in_(batch_ids))):
        expected = proof.scope.get('api')
        if expected and expected != raw.api_name:
            continue
        from app.macro.extensions.plan import digest
        if content_hash(raw.payload) != raw.content_hash and digest(raw.payload) != raw.content_hash:
            return Availability('unknown',reason='原件内容哈希不匹配')
        data = raw.payload.get('businessData',raw.payload)
        fields, rows = data.get('fields'),data.get('items')
        if not isinstance(fields,list) or not isinstance(rows,list):
            continue
        if key.subject_kind=='security' and isinstance(fields,list):
            identity_columns = [fields.index(k) for k in ('ts_code','o_code','n_code') if k in fields]
            if not identity_columns:
                continue
            rows = [r for r in rows if isinstance(r,list) and len(r)==len(fields)
                    and any(r[i]==key.subject_key for i in identity_columns)]
        if rows:
            return Availability('available',reason='已核对该对象的入库原件',source=proof.scope.get('source'))
    if proof.scope.get('confirmedEmpty'):
        return Availability('confirmed_empty',reason='该范围已完整检查，结果为空',source=proof.scope.get('source'))
    return Availability('missing',reason='原件中没有可用记录')
