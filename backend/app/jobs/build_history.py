from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from app.db.models import JobRun, QualityCheckResult
from app.marts.contracts import QualityResult


class SqlBuildHistory:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def start(self, options) -> str:
        run_id = str(uuid4())
        symbols = tuple(sorted(set(options.symbols)))
        scope = ",".join(symbols) or "all"
        business_key = f"{options.as_of.isoformat()}:{scope}"
        if len(business_key) > 256:
            digest = sha256(scope.encode("utf-8")).hexdigest()
            business_key = (
                f"{options.as_of.isoformat()}:symbols-{len(symbols)}:{digest}"
            )
        with self._session_factory.begin() as session:
            session.add(
                JobRun(
                    id=run_id,
                    job_name="build-and-publish",
                    business_key=business_key,
                    status="running",
                )
            )
        return run_id

    def record_quality(
        self,
        run_id: str,
        results: tuple[QualityResult, ...] | list[QualityResult],
    ) -> None:
        with self._session_factory.begin() as session:
            session.add_all(
                QualityCheckResult(
                    job_run_id=run_id,
                    check_name=result.name,
                    passed=result.passed,
                    details={"severity": result.severity, **result.details},
                )
                for result in results
            )

    def finish(
        self,
        run_id: str,
        status: str,
        message: str | None = None,
    ) -> None:
        with self._session_factory.begin() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise LookupError(f"build-and-publish job run not found: {run_id}")
            run.status = status
            run.error_message = message
            run.finished_at = datetime.now(timezone.utc)
