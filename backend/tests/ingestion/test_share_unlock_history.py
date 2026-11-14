from dataclasses import replace
from datetime import date

from sqlalchemy import select

from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.strategies import Dataset, SyncScope, TushareMode, build_request_plans
from app.db.models import Security, ShareUnlock, SourcePayload
from app.ingestion.pipeline import IngestionPipeline
from backend.tests.ingestion.test_pipeline import session_factory


def test_share_float_narrow_contract_preserves_legacy_history_and_raw():
    engine, factory = session_factory()
    plan, = build_request_plans(Dataset.SHARE_UNLOCK, TushareMode.STANDARD,
        SyncScope(start_date=date(2026, 9, 1), end_date=date(2026, 9, 5)))
    expected = ("ts_code", "ann_date", "float_date", "float_share")
    assert plan.fields == expected
    legacy = replace(plan, fields=(*expected, "unlock_date"))
    pipeline = IngestionPipeline(None, factory)
    values = ("600519.SH", "20260902", "20261001", 10)
    def persist(contract, fields, row):
        with factory.begin() as session:
            batch = pipeline.persist_response(contract, TushareResponse(fields=fields, items=(row,)), session=session)
            assert batch.row_count == 1 and batch.rejected_count == 0
    with factory.begin() as session:
        session.add(Security(ts_code=values[0], symbol="600519", name="贵州茅台"))
    persist(legacy, legacy.fields, (*values, "20261002"))
    # 当前请求收到额外列时，原始响应保留，Core 只写请求范围。
    persist(plan, legacy.fields, (*values[:3], 0, "20261003"))
    persist(plan, plan.fields, (*values[:3], 0))
    with factory() as session:
        row = session.scalars(select(ShareUnlock)).one()
        assert row.data["unlock_date"] == 20261002 and row.data["float_share"] == 0
        assert row.float_date == date(2026, 10, 1)
        raw = session.scalars(select(SourcePayload).order_by(SourcePayload.id)).all()
        assert [r.payload["fields"] for r in raw] == [list(legacy.fields), list(legacy.fields), list(plan.fields)]
        assert [r.payload["items"][0][-1] for r in raw] == ["20261002", "20261003", 0]
        assert raw[0].request_hash != raw[1].request_hash == raw[2].request_hash
    persist(plan, legacy.fields, (values[0], "20260903", "20261010", 11, "not-a-date"))
    with factory() as session:
        new = session.scalars(select(ShareUnlock).where(ShareUnlock.ann_date==date(2026,9,3))).one()
        assert "unlock_date" not in new.data and new.data["float_share"] == 11
    engine.dispose()
