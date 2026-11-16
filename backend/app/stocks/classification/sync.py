"""仅手动、有界的固定池分类同步；不生成派生指标或发布数据。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from threading import Lock
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.stocks.classification.contracts import Catalog, MEMBER_FIELDS
from app.stocks.classification.repository import ClassificationRepository, JOB_NAME
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.errors import TushareError
from app.connectors.tushare.strategies import Dataset, FIELD_REGISTRY
from app.db.models import JobRun, PublishedSnapshot
from app.ingestion.repositories import content_hash
from app.jobs.locks import JobLock
from app.shared.stock_mapping import ClassificationSyncConfig, StockMappingPolicy
from app.stocks.universes import UniverseResolver, UniverseSelection


class BudgetExhausted(Exception):
    pass


class RequestBudget:
    def __init__(self, max_requests: int):
        if isinstance(max_requests, bool) or max_requests <= 0:
            raise ValueError("request budget must be positive")
        self.limit = max_requests
        self.used = 0
        self._guard = Lock()

    def consume(self) -> None:
        with self._guard:
            if self.used >= self.limit:
                raise BudgetExhausted("physical HTTP request budget exhausted")
            self.used += 1


class ClassificationSync:
    def __init__(self, factory: sessionmaker[Session], client: TushareClient | None,
        budget: RequestBudget, selection: UniverseSelection, policy: StockMappingPolicy,
        lock: JobLock, *, market_years: int = 3):
        self.factory, self.client, self.budget = factory, client, budget
        self.selection, self.policy, self.lock = selection, policy, lock
        self.market_years = market_years
        self.repository = ClassificationRepository(factory)

    def preflight(self) -> dict[str, Any]:
        reference = UniverseResolver().resolve("representative-50")
        if (self.selection.profile != reference.profile or self.selection.version != reference.version
            or self.selection.fingerprint != reference.fingerprint or self.selection.symbols != reference.symbols):
            raise ValueError("only fixed representative-50@2026-09-19-v2 is supported")
        if (self.policy.classification.provider, self.policy.classification.source) != ("tushare", "SW2021"):
            raise ValueError("only tushare/SW2021 is supported")
        if not 1 <= self.market_years <= 10:
            raise ValueError("invalid market history years")
        as_of = self.policy.classification.sync.asOf
        if as_of is None:
            with self.factory() as session:
                pointer = session.get(PublishedSnapshot, "screener")
                if pointer is None:
                    raise ValueError("classification.sync.asOf required without screener publication")
                as_of = pointer.as_of.date()
        window_start = as_of.replace(year=as_of.year - self.market_years,
            day=min(as_of.day, 28) if as_of.month == 2 else as_of.day)
        mapping = self.policy.model_dump(mode="json", exclude={"classification": {"sync"}})
        contract = {"rules": "sw2021-members-v1", "pool": self.selection.to_payload(),
            "source": "SW2021", "asOf": as_of.isoformat(), "windowStart": window_start.isoformat(),
            "catalogApi": "index_classify", "catalogFields": FIELD_REGISTRY[Dataset.CLASSIFICATION],
            "memberApi": "index_member_all", "memberFields": MEMBER_FIELDS,
            "mappingHash": content_hash(mapping), "pageSize": self.policy.classification.sync.pageSize}
        # Warnings are runtime diagnostics, not stock pool identity.
        contract["pool"].pop("warnings", None)
        identity = content_hash(contract)
        control = self.repository.read_state(identity).get("observation", {})
        observation = control.get("identity", identity)
        state = self.repository.read_state(observation)
        units = [f"catalog:L{level}" for level in (1, 2, 3)] + [
            f"{symbol}:{flag}" for symbol in self.selection.symbols for flag in ("Y", "N")]
        return {**contract, "contractIdentity": identity, "observationIdentity": observation,
            "refreshObservation": control.get("refresh", False), "minimumRequests": 103,
            "maxRequestsPerRun": self.policy.classification.sync.maxRequestsPerRun,
            "remainingQueryUnits": [unit for unit in units if state.get(unit, {}).get("status") != "completed"],
            "units": units, "resume": state, "permission": "not_assessed", "publication": "unchanged"}

    def _fetch(self, identity: str, unit: str) -> dict[str, Any]:
        if self.client is None:
            raise ValueError("client required for collection")
        catalog = unit.startswith("catalog:")
        if catalog:
            params = {"src": "SW2021", "level": unit.split(":")[1]}
            fields = FIELD_REGISTRY[Dataset.CLASSIFICATION]
        else:
            symbol, flag = unit.split(":")
            params = {"ts_code": symbol, "is_new": flag}
            fields = MEMBER_FIELDS
        prior_count = self.budget.used
        try:
            response = self.client.call("index_classify" if catalog else "index_member_all", params, fields)
        except TushareError:
            from app.acquisition.adapters.classification import record_classification
            with self.repository.factory.begin() as session:
                record_classification(session,identity,'SW2021' if catalog else symbol,member=not catalog,
                                      error='CLASSIFICATION_SOURCE_FAILED')
            raise
        return self.repository.record_response(identity, unit, response, params, fields,
            page_size=self.policy.classification.sync.pageSize, observed_at=datetime.now(timezone.utc),
            request_count=self.budget.used - prior_count)

    def run(self, *, refresh: bool = False) -> dict[str, Any]:
        preflight = self.preflight()
        lock_key = "sync-classification:tushare:SW2021"
        if not self.lock.try_acquire(lock_key):
            return {"status": "locked", "requests": 0, "publication": "unchanged"}
        before = self.budget.used
        run_id = str(uuid4())
        result = {"status": "running", "requests": 0, "publication": "unchanged"}
        try:
            # Another observation may have finished between the read-only preflight and lock acquisition.
            preflight = self.preflight()
            if self.client is not None:
                self.client.set_before_request(self.budget.consume)
            identity = preflight["observationIdentity"]
            if refresh:
                identity = content_hash({"contract": preflight["contractIdentity"], "observation": str(uuid4())})
            if refresh or not self.repository.read_state(preflight["contractIdentity"]).get("observation"):
                with self.factory.begin() as session:
                    self.repository._set(session, preflight["contractIdentity"], "observation", {
                        "identity": identity, "refresh": refresh, "createdAt": datetime.now(timezone.utc).isoformat()})
            refresh = refresh or preflight["refreshObservation"]
            result.update({"identity": identity, "contractIdentity": preflight["contractIdentity"],
                "asOf": preflight["asOf"], "windowStart": preflight["windowStart"]})
            with self.factory.begin() as session:
                session.add(JobRun(id=run_id, job_name=JOB_NAME, business_key=identity, details=result))
            state = self.repository.read_state(identity)
            for unit in [f"catalog:L{level}" for level in (1, 2, 3)]:
                result["nextUnit"] = unit
                evidence = state.get(unit)
                if evidence is None or evidence["status"] == "failed":
                    evidence = self._fetch(identity, unit)
                if evidence["status"] != "completed":
                    result.update(status=evidence["status"], error=evidence.get("error"))
                    return result
            if state.get("catalog", {}).get("status") == "completed":
                with self.factory() as session:
                    rows = [row for level in (1, 2, 3)
                        for row in self.repository._response(session, state[f"catalog:L{level}"]).rows()]
                    catalog = Catalog.from_rows(rows)
            else:
                catalog = self.repository.commit_catalog(identity)
            stocks = {}
            for symbol in self.selection.symbols:
                state = self.repository.read_state(identity)
                if state.get("stock:" + symbol, {}).get("status") == "completed":
                    stocks[symbol] = state["stock:" + symbol]
                    continue
                for flag in ("Y", "N"):
                    unit = f"{symbol}:{flag}"
                    result["nextUnit"] = unit
                    evidence = state.get(unit)
                    if evidence is None or evidence["status"] == "failed":
                        evidence = self._fetch(identity, unit)
                    if evidence["status"] != "completed":
                        result.update(status=evidence["status"], error=evidence.get("error"))
                        return result
                stock = self.repository.commit_stock(identity, symbol, catalog,
                    date.fromisoformat(preflight["windowStart"]), date.fromisoformat(preflight["asOf"]), refresh=refresh)
                stocks[symbol] = stock
                if stock["status"] != "completed":
                    result.update(status=stock["status"], error=stock.get("error"), stocks=stocks)
                    return result
            result.update(status="completed", nextUnit=None, stocks=stocks,
                currentClassified=sum(stock["currentCoverage"] == "observed_classified" for stock in stocks.values()),
                observedMissing=sum(stock["currentCoverage"] == "observed_missing" for stock in stocks.values()),
                historyCompleteness="not_assessed", historyKind="history_reconstructed")
            return result
        except BudgetExhausted:
            result.update(status="partial", error="physical HTTP budget exhausted")
            return result
        except (TushareError, ValueError) as exc:
            result.update(status="failed", error=str(exc))
            return result
        finally:
            result["requests"] = self.budget.used - before
            try:
                if "identity" in result:
                    state = self.repository.read_state(result["identity"])
                    stocks = {}
                    for symbol in self.selection.symbols:
                        stock = dict(state.get("stock:" + symbol, {"status": "not_assessed", "currentCoverage": "not_assessed"}))
                        stock["queries"] = {flag: state.get(f"{symbol}:{flag}", {}).get("status", "not_assessed") for flag in ("Y", "N")}
                        stock["received"] = sum(state.get(f"{symbol}:{flag}", {}).get("received", 0) for flag in ("Y", "N"))
                        stocks[symbol] = stock
                    result["stocks"] = stocks
                    result["currentClassified"] = sum(stock["currentCoverage"] == "observed_classified" for stock in stocks.values())
                    result["observedMissing"] = sum(stock["currentCoverage"] == "observed_missing" for stock in stocks.values())
                    result["notAssessed"] = sum(stock["currentCoverage"] == "not_assessed" for stock in stocks.values())
                    result["catalog"] = state.get("catalog", {"status": "not_assessed"})
                with self.factory.begin() as session:
                    row = session.get(JobRun, run_id)
                    if row is not None:
                        row.status = result["status"] if result["status"] != "running" else "failed"
                        row.request_count = result["requests"]
                        row.row_count = sum(stock.get("retained", 0) for stock in result.get("stocks", {}).values())
                        row.finished_at = datetime.now(timezone.utc)
                        row.details = result
            finally:
                self.lock.release(lock_key)
