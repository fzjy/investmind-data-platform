"""仅聚合既有未验证库存，不初始化表、不读取Raw正文、不写状态。"""
from sqlalchemy import func, select

from app.db.models import IndexConstituent, SourcePayload
from app.macro.enterprise_target_preview import TARGET_EVIDENCE_APIS, TargetEvidenceInventory


def load_target_evidence_inventory(factory) -> TargetEvidenceInventory:
    counts = dict.fromkeys(TARGET_EVIDENCE_APIS, 0)
    with factory() as session:
        legacy = session.scalar(select(func.count()).select_from(IndexConstituent))
        for api, count in session.execute(select(SourcePayload.api_name, func.count())
            .where(SourcePayload.api_name.in_(TARGET_EVIDENCE_APIS)).group_by(SourcePayload.api_name)):
            counts[api] = count
    return TargetEvidenceInventory(legacy, counts)
