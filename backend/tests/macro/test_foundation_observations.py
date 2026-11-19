from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.db.models import MacroObservation


def test_narrow_macro_query_validates_only_candidate_imports(pg_factory, tmp_path, monkeypatch):
    from sqlalchemy import select
    from app.connectors.external_files.contracts import MacroImportRecord
    from app.db.models import ExternalRecord
    from app.macro.dataset_query import stored_observations
    from app.macro.import_lineage import is_verified_import
    from backend.tests.jobs.test_macro_offline_lineage import AS_OF, CATALOG, import_controlled_macro
    import_controlled_macro(pg_factory, tmp_path)
    series = CATALOG.series[0].id
    with pg_factory.begin() as session:
        record = session.scalar(select(ExternalRecord).where(ExternalRecord.payload["seriesId"].as_string() == series))
        payload = {k:v for k,v in record.payload.items() if k not in ("seriesId", "observedAt")}
        # 合法历史蛇形别名及零点时间戳应继续与同一观测期匹配。
        record.payload = {**payload, "series_id":series, "observed_at":"2026-08-01T00:00:00Z"}
        session.add(ExternalRecord(external_file_id=record.external_file_id,row_number=100,record_type="macro",
                                   payload={**payload, "series_id":series, "observed_at":"2025-01-01"}))
    validated = []
    original = MacroImportRecord.model_validate
    def validate(value):
        validated.append(value)
        return original(value)
    monkeypatch.setattr(MacroImportRecord,"model_validate",validate)
    with pg_factory() as session:
        row, = stored_observations(session,[series],AS_OF,start=date(2026,8,1),end=date(2026,8,1))
        assert is_verified_import(row)
    assert len(validated) == 1


def test_two_cutoffs_expiry_and_precision(pg_factory):
    from app.macro.datasets_routes import create_router
    with pg_factory.begin() as session:
        for revision, released, value, until in [
            ("a", 2, "0", None), ("b", 3, "1.2345678912", 4)
        ]:
            session.add(MacroObservation(source="tushare", series_id="cn.cpi.yoy", name="CPI",
                region="CN", frequency="monthly", unit="%", seasonal_adjustment="unknown",
                observed_at=date(2026,1,1), released_at=datetime(2026,released,1,tzinfo=UTC),
                valid_until=datetime(2026,until,1,tzinfo=UTC) if until else None,
                revision=revision, value=Decimal(value), source_url="https://tushare.pro"))
    app = FastAPI()
    app.include_router(create_router(pg_factory))
    with TestClient(app) as client:
        def read(cutoff):
            response = client.get("/api/v1/datasets/macro-observations", params={
                "series_id":"cn.cpi.yoy","start_date":"2026-01-01","end_date":"2026-01-01","as_of":cutoff})
            assert response.status_code == 200, response.text
            return response.json()["data"]
        assert read("2026-01-31T23:59:59Z") == []
        assert read("2026-02-01T08:00:00+08:00")[0]["value"] == 0
        row, = read("2026-03-01T00:00:00Z")
        assert row["revision"] == "b" and row["value"] == 1.2345678912
        assert read("2026-04-01T00:00:00Z") == []


def test_specialty_uses_completed_capture_time(pg_factory, tmp_path):
    from datetime import timedelta
    from app.macro.datasets_routes import create_router
    from backend.tests.macro.test_extension_view import NOW, fx, saved
    saved(pg_factory, tmp_path, "usdcnh", fx())
    app = FastAPI()
    app.include_router(create_router(pg_factory))
    with TestClient(app) as client:
        params = {"series_id":"fx.usdcnh","start_date":"2026-10-21","end_date":"2026-10-21"}
        url = "/api/v1/datasets/macro-specialty-observations"
        for elapsed in (0, 1, 2):
            result = client.get(url, params={**params, "as_of":(NOW+timedelta(seconds=elapsed)).isoformat()})
            assert result.status_code == 200
            if elapsed < 2:
                assert result.json()["data"] == []
            else:
                row, = result.json()["data"]
                assert row["quote_side"] == "bid_close" and row["unit"] == "CNH/USD"
                assert result.json()["meta"]["coverage_scope"] == "capture_as_of"
