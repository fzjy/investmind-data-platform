"""查询证据原子持久化；两类证据合并校验后才提交整股成员。"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.stocks.classification.contracts import Catalog, MemberRecord, MemberValidationError, merge_members, normalize_members
from app.connectors.tushare.contracts import TushareResponse
from app.db.models import Classification, ClassificationMember, IngestionBatch, JobCursor, SourcePayload
from app.ingestion.repositories import IngestionRepository, content_hash

JOB_NAME = "sync-classification"


class ClassificationRepository:
    def __init__(self, factory: sessionmaker[Session]):
        self.factory = factory

    def read_state(self, identity: str) -> dict[str, dict[str, Any]]:
        with self.factory() as session:
            return {row.cursor_key[len(identity) + 1:]: dict(row.cursor_value)
                for row in session.scalars(select(JobCursor).where(JobCursor.job_name == JOB_NAME,
                    JobCursor.cursor_key.startswith(identity + ":")))}

    @staticmethod
    def _set(session: Session, identity: str, unit: str, value: dict[str, Any]):
        key = identity + ":" + unit
        row = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME, JobCursor.cursor_key == key))
        if row is None:
            row = JobCursor(job_name=JOB_NAME, cursor_key=key)
            session.add(row)
        row.cursor_value = value
        row.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _response(session: Session, state: dict[str, Any]) -> TushareResponse:
        payload = session.get(SourcePayload, state["payloadId"])
        if payload is None or content_hash(payload.payload) != state["responseHash"]:
            raise ValueError("classification evidence/cursor mismatch")
        return TushareResponse.model_validate(payload.payload)

    def record_response(self, identity: str, unit: str, response: TushareResponse,
        params: dict[str, str], fields: tuple[str, ...], *, page_size: int,
        observed_at: datetime, request_count: int = 1) -> dict[str, Any]:
        with self.factory.begin() as session:
            prior = session.scalar(select(JobCursor).where(JobCursor.job_name == JOB_NAME,
                JobCursor.cursor_key == identity + ":" + unit))
            history = list(prior.cursor_value.get("previousAttempts", [])) if prior else []
            if prior:
                history.append({key: value for key, value in prior.cursor_value.items() if key != "previousAttempts"})
            repo = IngestionRepository(session)
            catalog = unit.startswith("catalog:")
            batch = repo.start_batch("classification" if catalog else "classification_member", "manual")
            payload = repo.save_payload(batch, "index_classify" if catalog else "index_member_all", params, fields, response)
            error = None
            status = "completed"
            try:
                if len(set(response.fields)) != len(response.fields) or not set(fields) <= set(response.fields):
                    raise ValueError("classification response fields mismatch")
                rows = response.rows()
                if catalog and (not rows or any(row.get("level") != params["level"] or row.get("src") != params["src"] for row in rows)):
                    raise ValueError("catalog level/source response mismatch")
                if len(response.items) >= page_size:
                    status = "incomplete"
                    error = "response reached cap; pagination not verified"
            except (ValueError, KeyError) as exc:
                status, error = "failed", str(exc)
            if error:
                repo.reject(batch, error)
            batch.request_count = request_count
            batch.row_count = len(response.items)
            batch.rejected_count = int(error is not None)
            batch.status = status
            batch.finished_at = observed_at
            state = {"status": status, "batchId": batch.id, "payloadId": payload.id,
                "responseHash": content_hash(payload.payload), "received": len(response.items),
                "params": params, "observedAt": observed_at.isoformat(), "error": error}
            state["previousAttempts"] = history
            if error:
                from app.acquisition.adapters.classification import record_classification
                record_classification(session,identity,'SW2021' if catalog else params['ts_code'],
                                      member=not catalog,error='CLASSIFICATION_REJECTED',references=({'batchId':batch.id},))
            self._set(session, identity, unit, state)
            return state

    def commit_catalog(self, identity: str) -> Catalog:
        state = self.read_state(identity)
        with self.factory.begin() as session:
            rows = []
            for level in (1, 2, 3):
                evidence = state.get(f"catalog:L{level}", {})
                if evidence.get("status") != "completed":
                    raise ValueError("catalog query incomplete")
                rows.extend(self._response(session, evidence).rows())
            catalog = Catalog.from_rows(rows)
            repo = IngestionRepository(session)
            existing_nodes = {node.classification_id: node for node in session.scalars(select(Classification).where(
                Classification.provider == "tushare", Classification.source == catalog.source))}
            nodes_by_id = {node.id: node for node in existing_nodes.values()}
            existing_members = session.scalars(select(ClassificationMember).where(
                ClassificationMember.classification_id.in_(nodes_by_id))).all()
            for member in existing_members:
                old_node = nodes_by_id[member.classification_id]
                old_path = []
                node = old_node
                while node is not None:
                    if node.classification_id in old_path:
                        raise ValueError("existing member catalog cycle")
                    old_path.insert(0, node.classification_id)
                    if node.parent_id is None:
                        break
                    node = existing_nodes.get(node.parent_id)
                    if node is None:
                        raise ValueError("existing member catalog parent missing")
                new_path = []
                node = catalog.nodes.get(old_node.classification_id)
                if node is None:
                    raise ValueError("existing member missing from new catalog")
                while node is not None:
                    new_path.insert(0, node["classification_id"])
                    node = catalog.nodes.get(node["parent_id"])
                if old_path != new_path or (member.data.get("path") and member.data["path"] != new_path):
                    raise ValueError("existing member path changed by catalog")
            for code, node in catalog.nodes.items():
                repo.upsert(Classification, {"provider": "tushare", "source": catalog.source, "classification_id": code}, node)
            self._set(session, identity, "catalog", {"status": "completed", "hash": content_hash(catalog.nodes), "nodes": len(catalog.nodes)})
            from app.acquisition.adapters.classification import record_classification
            record_classification(session,identity,catalog.source,member=False)
            return catalog

    def commit_stock(self, identity: str, symbol: str, catalog: Catalog, window_start: date,
        as_of: date, *, refresh: bool = False) -> dict[str, Any]:
        state = self.read_state(identity)
        key = "stock:" + symbol
        evidence_ids = [state.get(f"{symbol}:{flag}", {}).get("payloadId") for flag in ("Y", "N")]
        if key in state and (state[key]["status"] == "completed" or state[key].get("evidenceIds") == evidence_ids):
            return state[key]
        if any(state.get(f"{symbol}:{flag}", {}).get("status") != "completed" for flag in ("Y", "N")):
            return {"status": "not_assessed"}
        with self.factory.begin() as session:
            repo = IngestionRepository(session)
            # All validation precedes any core mutation, including existing history reconciliation.
            try:
                records = []
                for flag in ("Y", "N"):
                    records.extend(normalize_members(self._response(session, state[f"{symbol}:{flag}"]).rows(),
                        catalog=catalog, symbol=symbol, is_new=flag, window_start=window_start, as_of=as_of,
                        retain_outside_window=True))
                records = merge_members(records)
                nodes = {node.classification_id: node for node in session.scalars(select(Classification).where(
                    Classification.provider == "tushare", Classification.source == catalog.source))}
                by_id = {node.id: code for code, node in nodes.items()}
                existing = session.scalars(select(ClassificationMember).where(ClassificationMember.ts_code == symbol,
                    ClassificationMember.classification_id.in_(by_id))).all()
                by_key = {(by_id[row.classification_id], row.effective_from): row for row in existing}
                merged = []
                replacements = {(record.path[-1], record.effective_from): record for record in records}
                for row in existing:
                    code = by_id[row.classification_id]
                    node = catalog.nodes.get(code)
                    if node is None:
                        raise ValueError("existing member no longer matches catalog")
                    middle = catalog.nodes.get(node["parent_id"], {})
                    path = (middle.get("parent_id"), node["parent_id"], code)
                    if row.data.get("path") and tuple(row.data["path"]) != path:
                        raise ValueError("existing member path changed")
                    record = replacements.get((code, row.effective_from))
                    if record is not None:
                        if row.effective_to != record.effective_to and not (
                            refresh and row.effective_to is None and record.effective_to is not None
                            and tuple(row.data.get("path", ())) == record.path):
                            raise ValueError("conflicting existing member end")
                    else:
                        merged.append(MemberRecord(symbol, path, row.effective_from, row.effective_to, None, "", 0))
                merge_members([*merged, *records])
                records = [record for record in records if (record.path[-1], record.effective_from) in by_key
                    or record.effective_from <= as_of and (record.effective_to is None or record.effective_to >= window_start)]
            except (ValueError, KeyError, TypeError) as exc:
                flags = (exc.is_new,) if isinstance(exc, MemberValidationError) else ("Y", "N")
                result = {"status": "failed", "currentCoverage": "not_assessed", "error": str(exc),
                    "rejected": len(flags), "evidenceIds": evidence_ids}
                for flag in flags:
                    evidence = dict(state[f"{symbol}:{flag}"])
                    batch = session.get(IngestionBatch, evidence["batchId"])
                    repo.reject(batch, str(exc), row_number=exc.row_number if isinstance(exc, MemberValidationError) else None,
                        raw_record=exc.raw_record if isinstance(exc, MemberValidationError) else {
                            "symbol": symbol, "identity": identity, "evidenceIds": evidence_ids})
                    batch.rejected_count += 1
                    batch.status = "partial"
                    batch.row_count = 0
                    evidence.update(status="failed", error=str(exc))
                    self._set(session, identity, f"{symbol}:{flag}", evidence)
                from app.acquisition.adapters.classification import record_classification
                record_classification(session,identity,symbol,member=True,error='MEMBER_VALIDATION_FAILED')
                self._set(session, identity, key, result)
                return result
            for record in records:
                leaf = nodes[record.path[-1]]
                prior = by_key.get((record.path[-1], record.effective_from))
                evidence = state[f"{symbol}:{record.is_new}"]
                reference = {"identity": identity, "batchId": evidence["batchId"], "payloadId": evidence["payloadId"],
                    "rowNumber": record.row_number, "isNew": record.is_new, "observedAt": evidence["observedAt"]}
                data = dict(prior.data) if prior else {}
                references = list(data.get("evidence", []))
                if reference not in references:
                    references.append(reference)
                changes = list(data.get("changes", []))
                if prior and prior.effective_to != record.effective_to:
                    changes.append({"from": prior.effective_to.isoformat() if prior.effective_to else None,
                        "to": record.effective_to.isoformat(), "evidence": reference})
                data.update({"path": list(record.path), "inDate": record.effective_from.isoformat(),
                    "outDate": record.out_date.isoformat() if record.out_date else None,
                    "historyKind": "history_reconstructed", "sourceBinding": "matched_to_SW2021_catalog",
                    "evidence": references, "changes": changes})
                repo.upsert(ClassificationMember, {"classification_id": leaf.id, "ts_code": symbol,
                    "effective_from": record.effective_from}, {"effective_to": record.effective_to, "weight": None, "data": data})
            active = [record for record in records if record.effective_from <= as_of and (
                record.effective_to is None or record.effective_to >= as_of)]
            result = {"status": "completed", "currentCoverage": "observed_classified" if active else "observed_missing",
                "rejected": 0,
                "retained": len(records), "received": sum(state[f"{symbol}:{flag}"]["received"] for flag in ("Y", "N")),
                "historyKind": "history_reconstructed", "historyCompleteness": "not_assessed",
                "queries": {flag: "completed" for flag in ("Y", "N")},
                "observedGaps": [{"from": prior.effective_to.isoformat(), "to": current.effective_from.isoformat()}
                    for prior, current in zip(records, records[1:])
                    if prior.effective_to and (current.effective_from - prior.effective_to).days > 1]}
            from app.acquisition.adapters.classification import record_classification
            record_classification(session,identity,symbol,member=True,empty=not records,
                                  references=tuple({'rawId':i} for i in evidence_ids))
            self._set(session, identity, key, result)
            return result
