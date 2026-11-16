from datetime import date, datetime, timezone

import pytest
from sqlalchemy import event, func, select

from app.stocks.classification import repository
from app.stocks.classification.contracts import Catalog, MEMBER_FIELDS
from app.connectors.tushare.contracts import TushareResponse
from app.db.models import Classification, ClassificationMember, IngestionBatch, JobCursor, RejectedRecord, Security, SourcePayload
from backend.tests.classification.test_contracts import catalog_rows, member_row

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
IDENTITY = "a" * 64


def setup_repository(factory):
    with factory.begin() as session:
        session.add(Security(ts_code="600519.SH", symbol="600519", name="样本"))
    repo = repository.ClassificationRepository(factory)
    for index, row in enumerate(catalog_rows(), 1):
        fields = tuple(row)
        repo.record_response(IDENTITY, f"catalog:L{index}", TushareResponse(fields=fields, items=(tuple(row.values()),)),
            {"level": f"L{index}", "src": "SW2021"}, fields, page_size=2000, observed_at=NOW)
    catalog = repo.commit_catalog(IDENTITY)
    return factory, repo, catalog


def save(repo, rows, flag="Y", identity=IDENTITY, page_size=2000):
    return repo.record_response(identity, f"600519.SH:{flag}",
        TushareResponse(fields=MEMBER_FIELDS, items=tuple(tuple(row.get(field) for field in MEMBER_FIELDS) for row in rows)),
        {"ts_code": "600519.SH", "is_new": flag}, MEMBER_FIELDS, page_size=page_size, observed_at=NOW)


def commit(repo, catalog, identity=IDENTITY, refresh=False):
    return repo.commit_stock(identity, "600519.SH", catalog, date(2023, 9, 18), date(2026, 9, 18), refresh=refresh)


def test_two_units_required_before_stock_completion(pg_factory):
    factory, repo, catalog = setup_repository(pg_factory)
    save(repo, [member_row()])
    assert commit(repo, catalog)["status"] == "not_assessed"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ClassificationMember)) == 0
    save(repo, [], "N")
    assert commit(repo, catalog)["currentCoverage"] == "observed_classified"
    assert commit(repo, catalog)["status"] == "completed"
    with factory() as session:
        rows = session.scalars(select(ClassificationMember)).all()
        assert len(rows) == 1 and rows[0].weight is None
        assert rows[0].data["historyKind"] == "history_reconstructed"
        assert len(rows[0].data["evidence"]) == 1


def test_outside_pool_is_rejected_without_partial_member_writes(pg_factory):
    factory, repo, catalog = setup_repository(pg_factory)
    save(repo, [member_row(), member_row(ts_code="000001.SZ")])
    save(repo, [], "N")
    assert commit(repo, catalog)["status"] == "failed"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ClassificationMember)) == 0
        assert session.scalar(select(func.count()).select_from(SourcePayload)) == 5


def test_full_response_is_incomplete_and_not_committed_as_member(pg_factory):
    factory, repo, catalog = setup_repository(pg_factory)
    assert save(repo, [member_row()], page_size=1)["status"] == "incomplete"
    save(repo, [], "N")
    assert commit(repo, catalog)["status"] == "not_assessed"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ClassificationMember)) == 0


def test_failed_commit_preserves_pending_unit(pg_factory):
    factory, repo, _ = setup_repository(pg_factory)
    def fail(session):
        raise RuntimeError("simulated commit failure")
    event.listen(factory, "before_commit", fail)
    try:
        with pytest.raises(RuntimeError, match="simulated"):
            save(repo, [member_row()])
    finally:
        event.remove(factory, "before_commit", fail)
    assert "600519.SH:Y" not in repo.read_state(IDENTITY)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SourcePayload)) == 3


def test_refresh_closes_open_interval_with_evidence(pg_factory):
    factory, repo, catalog = setup_repository(pg_factory)
    save(repo, [member_row()]); save(repo, [], "N"); commit(repo, catalog)
    new_identity = "b" * 64
    save(repo, [], "Y", new_identity)
    save(repo, [member_row(out_date="20260918", is_new="N")], "N", new_identity)
    assert commit(repo, catalog, new_identity, True)["status"] == "completed"
    with factory() as session:
        row = session.scalar(select(ClassificationMember))
        assert row.effective_to == date(2026, 9, 17)
        assert len(row.data["evidence"]) == 2
        assert row.data["changes"][0]["from"] is None


def test_conflicting_refresh_is_atomic(pg_factory):
    factory, repo, catalog = setup_repository(pg_factory)
    save(repo, [member_row()]); save(repo, [], "N"); commit(repo, catalog)
    new_identity = "b" * 64
    save(repo, [member_row(in_date="20260902")], "Y", new_identity); save(repo, [], "N", new_identity)
    assert commit(repo, catalog, new_identity, True)["status"] == "failed"
    with factory() as session:
        row = session.scalar(select(ClassificationMember))
        assert row.effective_to is None
        assert len(row.data["evidence"]) == 1


def test_conflicting_current_and_history_reject_entire_stock(pg_factory):
    factory, repo, catalog = setup_repository(pg_factory)
    save(repo, [member_row()]); save(repo, [member_row(out_date="20260918", is_new="N")], "N")
    assert commit(repo, catalog)["status"] == "failed"
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(ClassificationMember)) == 0


def test_catalog_refresh_cannot_change_existing_member_parent_path(pg_factory):
    factory, repo, catalog = setup_repository(pg_factory)
    save(repo, [member_row()]); save(repo, [], "N"); commit(repo, catalog)
    rows = catalog_rows()
    rows.append(rows[0] | {"index_code": "801020.SI", "industry_code": "120000", "industry_name": "其他"})
    rows[1]["parent_code"] = "120000"
    identity = "d" * 64
    for level in (1, 2, 3):
        level_rows = [row for row in rows if row["level"] == f"L{level}"]
        fields = tuple(rows[0])
        repo.record_response(identity, f"catalog:L{level}", TushareResponse(fields=fields,
            items=tuple(tuple(row.get(field) for field in fields) for row in level_rows)),
            {"src": "SW2021", "level": f"L{level}"}, fields, page_size=2000, observed_at=NOW)
    with pytest.raises(ValueError, match="existing member"):
        repo.commit_catalog(identity)
    with factory() as session:
        middle = session.scalar(select(Classification).where(Classification.classification_id == "801011.SI"))
        assert middle.parent_id == "801010.SI"


def test_refresh_uses_window_external_closing_evidence_for_existing_open_member(pg_factory):
    factory, repo, catalog = setup_repository(pg_factory)
    save(repo, [member_row(in_date="20200101")]); save(repo, [], "N"); commit(repo, catalog)
    identity = "d" * 64
    save(repo, [member_row(in_date="20260901")], "Y", identity)
    save(repo, [member_row(in_date="20200101", out_date="20220101", is_new="N")], "N", identity)
    assert commit(repo, catalog, identity, True)["status"] == "completed"
    with factory() as session:
        rows = session.scalars(select(ClassificationMember).order_by(ClassificationMember.effective_from)).all()
        assert len(rows) == 2
        assert rows[0].effective_to == date(2021, 12, 31)
        assert len(rows[0].data["evidence"]) == 2


def test_rejection_is_attached_to_actual_response_and_row(pg_factory):
    factory, repo, catalog = setup_repository(pg_factory)
    y = save(repo, [member_row(), member_row(ts_code="000001.SZ")]); n = save(repo, [], "N")
    result = commit(repo, catalog)
    assert result["status"] == "failed" and result["rejected"] == 1
    with factory() as session:
        rejects = session.scalars(select(RejectedRecord)).all()
        assert len(rejects) == 1 and rejects[0].batch_id == y["batchId"] and rejects[0].row_number == 2
        assert session.get(IngestionBatch, y["batchId"]).rejected_count == 1
        assert session.get(IngestionBatch, n["batchId"]).rejected_count == 0
