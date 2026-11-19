"""用旧作业登记的指纹定位成功游标；每个分量仍须按完整冻结请求验原件。"""
import re

from sqlalchemy import select
from app.db.models import JobRun
from app.shared.errors import DataServiceError


def valid_fingerprint(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def job_fingerprints(session, job, day, checked_at):
    suffix = ":" + day.isoformat()
    keys = session.scalars(select(JobRun.business_key).where(
        JobRun.job_name == job, JobRun.business_key.endswith(suffix),
        JobRun.started_at <= checked_at).distinct())
    return {key[:-len(suffix)] for key in keys if valid_fingerprint(key[:-len(suffix)])}


def legacy_candidate_catalog(catalog):
    """旧直写可借现行目录重算候选；已登记的历史指纹仍可单独核验。"""
    if catalog is not None:
        return catalog
    from app.market.data.catalog import MarketDataCatalog
    try:
        return MarketDataCatalog.load()
    except (OSError, ValueError):
        return None


def choose_contract(candidates):
    """并列组必须有一组完整包含其余组的相同事实，禁止跨指纹补行或拼接分量。"""
    if not candidates:
        return None
    rank = max(priority for priority, _, _ in candidates)
    tied = [(values, result) for priority, values, result in candidates if priority == rank]
    reference, result = max(tied, key=lambda item: len(item[0]))
    for values, _ in tied:
        if any(reference.get(key) != value for key, value in values.items()):
            raise DataServiceError("INVALID_SOURCE_PAYLOAD", "同一时点存在冲突的市场采集凭证")
    return result
