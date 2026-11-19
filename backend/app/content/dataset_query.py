"""共享内容事实的发布时间顺序与可展示范围。"""
from datetime import datetime

from sqlalchemy import cast, select
from sqlalchemy.dialects.postgresql import JSONB

from app.content.content_policy import DISPLAY_STATUSES
from app.db.models import ContentDocument


def visible_documents(through: datetime, *, content_type=None, symbol=None, published_from=None):
    doc = ContentDocument
    statement = select(doc).where(doc.copyright_status.in_(DISPLAY_STATUSES), doc.published_at <= through)
    if content_type is not None:
        statement = statement.where(doc.content_type == content_type)
    if symbol is not None:
        statement = statement.where(cast(doc.securities, JSONB).contains([symbol]))
    if published_from is not None:
        statement = statement.where(doc.published_at >= published_from)
    return statement.order_by(doc.published_at.desc(), doc.id.desc())
