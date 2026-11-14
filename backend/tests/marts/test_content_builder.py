from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import ExternalBase
from app.db.models import ContentDocument
from app.content.marts.content import ContentMartBuilder, ContentRepository


def test_unknown_copyright_is_not_displayed() -> None:
    engine = create_engine("sqlite:///:memory:", execution_options={"schema_translate_map": {"raw": None, "core": None}})
    ExternalBase.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory.begin() as session:
        session.add(ContentDocument(source="x", dedupe_key="x", content_type="news", title="secret", published_at=datetime.now(timezone.utc), source_url="https://example.com", securities=[], copyright_status="unknown"))
    assert ContentMartBuilder(ContentRepository(factory)).build(datetime.now(timezone.utc)) == []
