"""Controlled JSONL -> SQLite -> real builder/service DTO for frontend rendering tests."""
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
from sqlalchemy import delete
from app.db.models import MacroObservation

from app.macro.database_provider import MacroDatabaseProvider
from app.macro.service import MacroService
from app.macro.jobs.macro_publish import MacroPublishJob
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.marts.publication import SnapshotPublisher
from app.shared.repository import SnapshotRepository
from backend.tests.jobs.test_build_and_publish_integration import _factory
from backend.tests.jobs.test_macro_offline_lineage import CATALOG, import_controlled_macro
from backend.tests.jobs.test_macro_publish import History


def main() -> None:
    output: dict[str, object] = {}
    for label in ("full", "partial"):
        factory = _factory()
        with tempfile.TemporaryDirectory(prefix="macro-dto-") as temporary:
            import_controlled_macro(factory, Path(temporary), 20)
            if label == "partial":
                with factory.begin() as session:
                    session.execute(delete(MacroObservation).where(MacroObservation.series_id.in_(
                        ("cn.cpi.core_yoy", "cn.rate.dr007", "cn.money.m1_yoy", "us.cpi.core_yoy"))))
            publisher = SnapshotPublisher(factory)
            report = MacroPublishJob(MacroMartBuilder(MacroRepository(factory)), publisher, History()).publish(
                "macro-run", datetime(2026, 8, 24, tzinfo=UTC), CATALOG, (),
            )
            assert report.status == "completed", report.quality
            service = MacroService(MacroDatabaseProvider(SnapshotRepository(factory)))
            output[label] = {
                "transmission": service.get_transmission().model_dump(mode="json", by_alias=True),
                "insights": service.get_insights().model_dump(mode="json", by_alias=True),
            }
        factory.kw["bind"].dispose()
    # Isolated Python (-I) ignores PYTHONIOENCODING; ASCII JSON is lossless even
    # when Windows redirects stdout using a legacy code page.
    print(json.dumps(output, ensure_ascii=True))


if __name__ == "__main__":
    main()
