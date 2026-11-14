from datetime import date
import json

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Classification, ClassificationMember, Security
from app.ingestion.normalizers import normalize_classification
from app.ingestion.pipeline import IngestionPipeline
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.strategies import Dataset, RequestPlan
from backend.tests.derived.test_inputs import _factory


def test_same_code_different_sources_keep_original_ids():
    factory = _factory()
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="样本"))
        old = Classification(provider="tushare", source="SW2014", classification_id="801010.SI",
            name="旧行业", level=1, data={"src": "SW2014"})
        session.add(old)
        session.flush()
        old_id = old.id
        session.add(ClassificationMember(classification_id=old_id, ts_code="600519.SH",
            effective_from=date(2020, 1, 1)))
        session.add(Classification(provider="tushare", source="SW2021", classification_id="801010.SI",
            name="新行业", level=1, data={"src": "SW2021"}))
    with factory() as session:
        nodes = session.scalars(select(Classification)).all()
        assert len(nodes) == 2
        assert session.get(Classification, old_id).name == "旧行业"
        assert session.get(Classification, old_id).data == {"src": "SW2014"}
        assert session.scalar(select(ClassificationMember)).classification_id == old_id


def test_explicit_request_source_rejects_mismatch():
    with pytest.raises(ValueError, match="source"):
        normalize_classification({"index_code": "801010.SI", "industry_name": "行业", "src": "SW2014"}, source="SW2021")


def test_missing_source_never_guesses_sw2021():
    assert normalize_classification({"index_code": "801010.SI", "industry_name": "行业"})["source"] == "legacy_unknown"


def test_generic_ingestion_never_overwrites_other_source():
    factory = _factory()
    def reply(request):
        source = json.loads(request.content)["params"]["src"]
        return httpx.Response(200, json={"code": 0, "data": {"fields": ["index_code", "industry_name", "level"],
            "items": [["801010.SI", source, "L1"]]}})
    with TushareClient("test-token", transport=httpx.MockTransport(reply)) as client:
        for source in ("SW2014", "SW2021", "SW2021"):
            result = IngestionPipeline(client, factory).run(RequestPlan(Dataset.CLASSIFICATION, "index_classify",
                {"src": source}, ("index_code", "industry_name", "level"), "test:" + source))
            assert result.row_count == 1 and result.rejected_count == 0
    with factory() as session:
        nodes = session.scalars(select(Classification).order_by(Classification.id)).all()
        assert [(node.source, node.name) for node in nodes] == [("SW2014", "SW2014"), ("SW2021", "SW2021")]


def test_generic_request_source_mismatch_is_quarantined():
    factory = _factory()
    def reply(request):
        return httpx.Response(200, json={"code": 0, "data": {"fields": ["index_code", "industry_name", "src"],
            "items": [["801010.SI", "行业", "SW2014"]]}})
    with TushareClient("test-token", transport=httpx.MockTransport(reply)) as client:
        result = IngestionPipeline(client, factory).run(RequestPlan(Dataset.CLASSIFICATION, "index_classify",
            {"src": "SW2021"}, ("index_code", "industry_name", "src"), "test:SW2021"))
    assert result.row_count == 0 and result.rejected_count == 1
    with factory() as session:
        assert session.scalars(select(Classification)).all() == []
