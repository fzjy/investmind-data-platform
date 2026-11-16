"""为模拟其他发布者的测试建立可引用的快照历史。"""
from datetime import datetime

from sqlalchemy import select
from app.db.models import PageSnapshot, PublishedSnapshot
from sqlalchemy.orm import Session


def set_snapshot_version(session: Session, pointer: PublishedSnapshot, version: str, *,
                         as_of: datetime | None = None) -> None:
    if as_of is not None:
        pointer.as_of = as_of
    session.add(PageSnapshot(product=pointer.product, snapshot_version=version,
                             as_of=pointer.as_of, payload=pointer.payload))
    session.flush()
    pointer.version = version
    if as_of is not None:
        session.flush()
        assert session.scalar(select(PageSnapshot.as_of).filter_by(
            product=pointer.product, snapshot_version=version)) == as_of
        assert session.scalar(select(PublishedSnapshot.as_of).filter_by(
            product=pointer.product, version=version)) == as_of
