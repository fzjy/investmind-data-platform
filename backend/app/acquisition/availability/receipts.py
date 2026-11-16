from sqlalchemy import select
from app.acquisition.contracts import Availability
from app.db.models import SourcePayload
from app.ingestion.repositories import content_hash


def raw_availability(session,key,proof):
    """只检查该对象最近成功凭证所引用的原件。"""
    ids = [r['batchId'] for r in proof.references if r.get('batchId')]
    if not ids:
        return None
    for raw in session.scalars(select(SourcePayload).where(SourcePayload.batch_id.in_(ids))):
        if content_hash(raw.payload) != raw.content_hash:
            return Availability('unknown',reason='原件内容哈希不匹配')
        fields, rows = raw.payload.get('fields'),raw.payload.get('items')
        if not isinstance(fields,list) or not isinstance(rows,list):
            continue
        if rows:
            return Availability('available',reason='已核对该对象的入库原件',source=proof.scope.get('source'))
    if proof.scope.get('confirmedEmpty'):
        return Availability('confirmed_empty',reason='该范围已完整检查，结果为空',source=proof.scope.get('source'))
    return Availability('missing',reason='原件中没有可用记录')
