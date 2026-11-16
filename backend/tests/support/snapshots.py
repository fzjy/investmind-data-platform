"""为模拟其他发布者的测试建立可引用的快照历史。"""
from app.db.models import PageSnapshot, PublishedSnapshot
from sqlalchemy.orm import Session


def set_snapshot_version(session: Session, pointer: PublishedSnapshot, version: str) -> None:
    session.add(PageSnapshot(product=pointer.product, snapshot_version=version,
                             as_of=pointer.as_of, payload=pointer.payload))
    session.flush()
    pointer.version = version
