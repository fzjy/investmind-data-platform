from dataclasses import replace
from datetime import date
import json

import httpx
import pytest
from sqlalchemy import func, select

from app.stocks.classification import sync
from app.connectors.tushare.client import TushareClient
from app.db.models import JobCursor, JobRun, Security
from app.jobs.locks import ProcessLocalJobLock
from app.shared.stock_mapping import StockMappingPolicy
from app.stocks.universes import UniverseResolver

from backend.tests.classification.test_contracts import catalog_rows, member_row


def policy(**changes):
    base = StockMappingPolicy.load()
    return base.model_copy(update={"classification": base.classification.model_copy(update={"sync":
        sync.ClassificationSyncConfig(asOf=date(2026, 9, 18), **changes)})})


def task(new_pg_factory, *, max_requests=128, selected=None, mapping=None, reply=None):
    factory = new_pg_factory()
    selection = selected or UniverseResolver().resolve("representative-50")
    with factory.begin() as session:
        session.add_all([Security(ts_code=code, symbol=code[:6], name="样本") for code in selection.symbols])
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if reply:
            override = reply(body, len(calls))
            if override is not None:
                return override
        fields = body["fields"].split(",")
        if body["api_name"] == "index_classify":
            rows = [row for row in catalog_rows() if row["level"] == body["params"]["level"]]
        else:
            rows = []
        return httpx.Response(200, json={"code": 0, "data": {"fields": fields,
            "items": [[row.get(field) for field in fields] for row in rows]}})
    budget = sync.RequestBudget(max_requests)
    client = TushareClient("test-token", transport=httpx.MockTransport(handler), max_retries=2,
        backoff_seconds=0, before_request=budget.consume)
    job = sync.ClassificationSync(factory, client, budget, selection, mapping or policy(), ProcessLocalJobLock())
    return job, factory, calls, client


@pytest.mark.parametrize("network", [False, True])
def test_physical_retries_share_budget_before_post(new_pg_factory, network):
    def failure(body, count):
        if network:
            raise httpx.ConnectError("network unavailable")
        return httpx.Response(503)
    job, _, calls, client = task(new_pg_factory, max_requests=2, reply=failure)
    try:
        result = job.run()
        assert result["status"] == "partial" and result["requests"] == 2
        assert len(calls) == 2
        assert result["nextUnit"] == "catalog:L1"
    finally:
        client.close()


def test_budget_partial_resume_and_completed_restart_zero_http(new_pg_factory):
    job, factory, calls, client = task(new_pg_factory, max_requests=4)
    try:
        first = job.run()
        assert first["status"] == "partial" and len(calls) == 4
        new_budget = sync.RequestBudget(128)
        client._before_request = new_budget.consume
        second = sync.ClassificationSync(factory, client, new_budget, job.selection, job.policy, job.lock).run()
        assert second["status"] == "completed" and second["requests"] == 99
        assert len(calls) == 103
        before = len(calls)
        result = job.run()
        assert result["status"] == "completed" and result["requests"] == 0
        assert len(calls) == before
        assert result["currentClassified"] == 0 and result["observedMissing"] == 50
    finally:
        client.close()


def test_dry_run_is_read_only_and_reports_lower_bound(new_pg_factory):
    job, factory, calls, client = task(new_pg_factory)
    try:
        result = job.preflight()
        assert result["minimumRequests"] == 103 and result["pool"]["size"] == 50
        assert result["maxRequestsPerRun"] == 128 and result["source"] == "SW2021"
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(JobRun)) == 0
            assert session.scalar(select(func.count()).select_from(JobCursor)) == 0
        assert calls == []
    finally:
        client.close()


def test_budget_change_preserves_identity_but_contract_change_isolates(new_pg_factory):
    job, _, _, client = task(new_pg_factory)
    try:
        original = job.preflight()["contractIdentity"]
        job.policy = policy(maxRequestsPerRun=64)
        assert job.preflight()["contractIdentity"] == original
        job.policy = policy(pageSize=1000)
        assert job.preflight()["contractIdentity"] != original
        job.policy = policy().model_copy(update={"policyVersion": "another-policy"})
        assert job.preflight()["contractIdentity"] != original
    finally:
        client.close()


@pytest.mark.parametrize("profile", ["all", "custom"])
def test_non_fixed_pool_fails_before_http(new_pg_factory, profile):
    selection = replace(UniverseResolver().resolve("representative-50"), profile=profile)
    job, _, calls, client = task(new_pg_factory, selected=selection)
    try:
        with pytest.raises(ValueError, match="representative-50"):
            job.preflight()
        assert calls == []
    finally:
        client.close()


def test_missing_as_of_fails_without_guessing_calendar(new_pg_factory):
    job, _, calls, client = task(new_pg_factory, mapping=StockMappingPolicy.load())
    try:
        with pytest.raises(ValueError, match="asOf"):
            job.preflight()
        assert calls == []
    finally:
        client.close()


def test_lock_contention_zero_http(new_pg_factory):
    job, _, calls, client = task(new_pg_factory)
    assert job.lock.try_acquire("sync-classification:tushare:SW2021")
    try:
        assert job.run()["status"] == "locked"
        assert calls == []
    finally:
        job.lock.release("sync-classification:tushare:SW2021")
        client.close()


def test_authentication_failure_stops_immediately(new_pg_factory):
    job, _, calls, client = task(new_pg_factory, reply=lambda body, count: httpx.Response(403))
    try:
        assert job.run()["status"] == "failed"
        assert len(calls) == 1
    finally:
        client.close()


def test_partial_report_has_all_stock_units_without_claiming_coverage(new_pg_factory):
    job, _, _, client = task(new_pg_factory, max_requests=4)
    try:
        result = job.run()
        assert len(result["stocks"]) == 50
        first = job.selection.symbols[0]
        assert result["stocks"][first]["queries"] == {"Y": "completed", "N": "not_assessed"}
        assert result["stocks"][first]["currentCoverage"] == "not_assessed"
        assert result["currentClassified"] == 0 and result["notAssessed"] == 50
    finally:
        client.close()


def test_client_without_budget_hook_cannot_issue_unmetered_http(new_pg_factory):
    job, _, calls, client = task(new_pg_factory, max_requests=1)
    client._before_request = None
    try:
        result = job.run()
        assert len(calls) <= 1
        assert result["status"] != "completed"
    finally:
        client.close()


def test_failed_query_is_retried_on_normal_resume_without_repeating_completed_units(new_pg_factory):
    def bad_first_stock(body, count):
        if count == 4:
            return httpx.Response(200, json={"code": 0, "data": {"fields": ["ts_code"], "items": []}})
    job, _, calls, client = task(new_pg_factory, reply=bad_first_stock)
    try:
        assert job.run()["status"] == "failed"
        result = job.run()
        assert result["status"] == "completed"
        assert len([call for call in calls if call["api_name"] == "index_classify"]) == 3
        assert len(calls) == 104
        history = job.repository.read_state(result["identity"])[job.selection.symbols[0] + ":Y"]["previousAttempts"]
        assert len(history) == 1 and history[0]["status"] == "failed"
    finally:
        client.close()
