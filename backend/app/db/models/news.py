from datetime import datetime
from sqlalchemy import JSON, DateTime, String, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class NewsDelivery(Base):
    __tablename__ = 'news_delivery'
    __table_args__ = (Index('ix_news_delivery_source_external', 'source', 'external_id', 'id'),
                     Index('ix_news_delivery_pending', 'source', 'delivered', 'id'), {'schema': 'ops'})
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    delivered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
