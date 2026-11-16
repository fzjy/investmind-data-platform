from datetime import datetime, timezone


from app.db.models import ContentDocument
from app.content.marts.content import ContentMartBuilder, ContentRepository


def test_unknown_copyright_is_not_displayed(pg_factory) -> None:
    factory = pg_factory
    with factory.begin() as session:
        session.add(ContentDocument(source="x", dedupe_key="x", content_type="news", title="secret", published_at=datetime.now(timezone.utc), source_url="https://example.com", securities=[], copyright_status="unknown"))
    assert ContentMartBuilder(ContentRepository(factory)).build(datetime.now(timezone.utc)) == []
