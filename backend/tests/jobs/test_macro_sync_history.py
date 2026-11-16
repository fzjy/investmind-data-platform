from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.db.models import JobFailure, JobRun
from app.jobs.locks import macro_sync_lock_key
from app.macro.jobs.macro_sync_history import (
    MacroCatalogVersionHashMismatch,
    SqlMacroSyncHistory,
    macro_sync_business_key,
)


AS_OF = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
VERSION = "2026-09-20-v1"
HASH = "a" * 64
V7_VERSION = "2026-09-30-v7"
V7_LF_HASH = "0840df6a99c8b7f197557dce1a68fe3afe5ae1f30c042541dcd24949b3147e9a"
V7_LEGACY_HASHES = (
    "85c55cd9da189b751cafd3736d47095e920c0f8c6a7bac9db3ffa177c4562a31",
    "7c4787cfb46c09b4491c600d28ae27696259c6604e9741626b1367d3500f3fde",
)


def make_history(pg_factory):
    engine, factory = pg_factory.kw["bind"], pg_factory
    return engine, factory, SqlMacroSyncHistory(factory)


def test_business_and_lock_keys_are_stable() -> None:
    key = macro_sync_business_key(AS_OF, VERSION, "Asia/Shanghai")
    assert key == "2026-W38:2026-09-20-v1"
    assert macro_sync_lock_key(key) == "sync-macro:2026-W38:2026-09-20-v1"


def test_completed_skip_partial_retry_and_force_attempt_numbers(pg_factory) -> None:
    engine, factory, history = make_history(pg_factory)
    try:
        key = macro_sync_business_key(AS_OF, VERSION, "Asia/Shanghai")
        assert history.inspect(key, VERSION, HASH) == (False, 1)
        first = history.start(key, AS_OF, VERSION, HASH, "cli", 1)
        history.finish(first, "partial", {"planned": 20, "requestCount": 1})
        assert history.inspect(key, VERSION, HASH) == (False, 2)
        second = history.start(key, AS_OF, VERSION, HASH, "scheduler", 2)
        history.finish(second, "completed", {"planned": 20})
        assert history.inspect(key, VERSION, HASH) == (True, 3)
        forced = history.start(key, AS_OF, VERSION, HASH, "cli", 3)
        with factory() as session:
            rows = session.scalars(select(JobRun).order_by(JobRun.started_at)).all()
        assert [row.details["attempt"] for row in rows] == [1, 2, 3]
        assert all(row.job_name == "sync-macro" and row.business_key == key for row in rows)
        assert rows[-1].id == forced
    finally:
        engine.dispose()


def test_version_hash_conflict_is_rejected_before_new_run(pg_factory) -> None:
    engine, factory, history = make_history(pg_factory)
    try:
        key = macro_sync_business_key(AS_OF, VERSION, "Asia/Shanghai")
        history.start(key, AS_OF, VERSION, HASH, "cli", 1)
        with pytest.raises(MacroCatalogVersionHashMismatch, match="MACRO_CATALOG_VERSION_HASH_MISMATCH"):
            history.inspect(key, VERSION, "b" * 64)
        with factory() as session:
            assert len(session.scalars(select(JobRun)).all()) == 1
    finally:
        engine.dispose()


def test_terminal_lock_and_failure_are_redacted(pg_factory) -> None:
    engine, factory, history = make_history(pg_factory)
    try:
        key = macro_sync_business_key(AS_OF, VERSION, "Asia/Shanghai")
        run_id = history.record_terminal(key, AS_OF, VERSION, HASH, "cli", 1, "locked")
        history.record_failure(run_id, "cn.cpi.yoy", "fred", "TIMEOUT", "api_key=hidden-value", True)
        with factory() as session:
            run = session.get(JobRun, run_id)
            failure = session.scalar(select(JobFailure))
        assert run is not None and run.status == "locked" and run.finished_at is not None
        assert failure is not None and "hidden-value" not in failure.message
        assert failure.error_code == "TIMEOUT"
    finally:
        engine.dispose()


def test_direct_history_failure_rejects_uppercase_secret_shaped_code(pg_factory) -> None:
    engine, factory, history = make_history(pg_factory)
    try:
        key = macro_sync_business_key(AS_OF, VERSION, "Asia/Shanghai")
        run_id = history.start(key, AS_OF, VERSION, HASH, "cli", 1)
        history.record_failure(
            run_id, "us.cpi.yoy", "fred", "FREDAPIKEYTESTTOKEN",
            "credential FREDAPIKEYTESTTOKEN", False,
        )
        with factory() as session:
            failure = session.scalar(select(JobFailure))
        assert failure is not None
        assert failure.error_code == "MACRO_SOURCE_ERROR"
        assert failure.message == "MACRO_SOURCE_ERROR"
    finally:
        engine.dispose()


def test_failure_without_gap_snapshot_keeps_last_explicit_snapshot(pg_factory) -> None:
    engine, _, history = make_history(pg_factory)
    try:
        first = history.start("2026-W38:" + VERSION, AS_OF, VERSION, HASH, "cli", 1)
        history.finish(first, "partial", {
            "unresolvedGaps": {
                "us.cpi.yoy": [{"start": "2020-01-01", "end": "2020-01-03"}]
            }
        })
        second = history.start("2026-W39:" + VERSION, AS_OF, VERSION, HASH, "cli", 1)
        history.finish(second, "failed", {"errorCode": "MACRO_SYNC_FAILED"})
        assert history.unresolved_gaps(VERSION, HASH) == {
            "us.cpi.yoy": ((date(2020, 1, 1), date(2020, 1, 3)),)
        }
    finally:
        engine.dispose()


@pytest.mark.parametrize("legacy_hash", V7_LEGACY_HASHES)
def test_verified_newline_legacy_identity_preserves_runs_and_gap_snapshot(pg_factory, legacy_hash):
    engine, factory, history = make_history(pg_factory)
    try:
        key = macro_sync_business_key(AS_OF, V7_VERSION, "Asia/Shanghai")
        run_id = history.start(key, AS_OF, V7_VERSION, legacy_hash, "cli", 1)
        history.finish(run_id, "partial", {"unresolvedGaps": {
            "cn.cpi.core_yoy": [{"start": "2016-09-30", "end": "2024-11-30"}]
        }})
        with factory() as session:
            before = dict(session.get(JobRun, run_id).details)
        assert history.inspect(key, V7_VERSION, V7_LF_HASH) == (False, 2)
        assert history.unresolved_gaps(V7_VERSION, V7_LF_HASH) == {
            "cn.cpi.core_yoy": ((date(2016, 9, 30), date(2024, 11, 30)),)
        }
        with factory() as session:
            assert session.get(JobRun, run_id).details == before
            assert len(session.scalars(select(JobRun)).all()) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("expected_hash", (HASH, "b" * 64))
def test_legacy_identity_is_not_a_bypass_for_changed_content(pg_factory, expected_hash):
    engine, factory, history = make_history(pg_factory)
    try:
        run_id = history.start("prior", AS_OF, V7_VERSION, V7_LEGACY_HASHES[1], "cli", 1)
        history.finish(run_id, "partial", {"unresolvedGaps": {
            "cn.cpi.core_yoy": [{"start": "2016-09-30", "end": "2024-11-30"}]
        }})
        with pytest.raises(MacroCatalogVersionHashMismatch):
            history.inspect("next", V7_VERSION, expected_hash)
        assert history.unresolved_gaps(V7_VERSION, expected_hash) == {}
        with factory() as session:
            assert session.get(JobRun, run_id).details["catalogHash"] == V7_LEGACY_HASHES[1]
    finally:
        engine.dispose()


@pytest.mark.parametrize("recorded_hash", (None, {}, HASH, V7_LEGACY_HASHES[1].upper()))
def test_unknown_or_malformed_legacy_hash_fails_closed(pg_factory, recorded_hash):
    engine, factory, history = make_history(pg_factory)
    try:
        with factory.begin() as session:
            session.add(JobRun(id="bad-hash", job_name="sync-macro", business_key="prior", status="partial",
                details={"catalogVersion": V7_VERSION, "catalogHash": recorded_hash, "unresolvedGaps": {
                    "cn.cpi.core_yoy": [{"start": "2016-09-30", "end": "2024-11-30"}]
                }}))
        with pytest.raises(MacroCatalogVersionHashMismatch):
            history.inspect("next", V7_VERSION, V7_LF_HASH)
        assert history.unresolved_gaps(V7_VERSION, V7_LF_HASH) == {}
    finally:
        engine.dispose()


@pytest.mark.parametrize('legacy_hash', (V7_LF_HASH, *V7_LEGACY_HASHES))
def test_v8_inherits_only_unchanged_series_gaps_without_inheriting_completed(pg_factory, legacy_hash):
    from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
    active = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    engine, factory, history = make_history(pg_factory)
    try:
        run_id = history.start('old-week', AS_OF, V7_VERSION, legacy_hash, 'cli', 1)
        history.finish(run_id, 'completed', {'unresolvedGaps': {
            'cn.cpi.core_yoy': [{'start': '2020-01-01', 'end': '2020-02-01'}],
            'cn.rate.govbond_10y': [{'start': '2020-01-01', 'end': '2020-02-01'}],
        }})
        with factory() as session:
            before = dict(session.get(JobRun, run_id).details)
        assert history.unresolved_gaps(active.catalog_version, active.catalog_hash) == {
            'cn.cpi.core_yoy': ((date(2020, 1, 1), date(2020, 2, 1)),)
        }
        assert history.inspect(macro_sync_business_key(AS_OF, active.catalog_version, 'Asia/Shanghai'),
            active.catalog_version, active.catalog_hash) == (False, 1)
        assert SqlMacroSyncHistory(factory, job_name='sync-govbond').unresolved_gaps(
            active.catalog_version, active.catalog_hash) == {}
        assert history.unresolved_gaps(active.catalog_version, 'b' * 64) == {}
        with factory() as session:
            assert session.get(JobRun, run_id).details == before
        new_run = history.start('new-week', AS_OF, active.catalog_version, active.catalog_hash, 'cli', 1)
        history.finish(new_run, 'completed', {'unresolvedGaps': {}})
        assert history.unresolved_gaps(active.catalog_version, active.catalog_hash) == {}
    finally:
        engine.dispose()


def test_verified_hash_does_not_carry_completed_or_gaps_across_catalog_versions(pg_factory):
    engine, _, history = make_history(pg_factory)
    try:
        run_id = history.start("prior", AS_OF, "older-version", V7_LEGACY_HASHES[1], "cli", 1)
        history.finish(run_id, "completed", {"unresolvedGaps": {
            "cn.cpi.core_yoy": [{"start": "2016-09-30", "end": "2024-11-30"}]
        }})
        assert history.inspect("next", V7_VERSION, V7_LF_HASH) == (False, 1)
        assert history.unresolved_gaps(V7_VERSION, V7_LF_HASH) == {}
    finally:
        engine.dispose()


@pytest.mark.parametrize('snapshot', ['none','empty','gaps'])
def test_v9_prefers_v8_explicit_snapshot_then_v7_without_reviving_changed_contract(pg_factory, snapshot):
    from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
    from app.macro.history_catalog import V8_HASH, V8_VERSION
    active=MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    engine,factory,history=make_history(pg_factory)
    try:
        old=history.start('v7-old',AS_OF,V7_VERSION,V7_LF_HASH,'cli',1)
        history.finish(old,'completed',{'unresolvedGaps':{'cn.cpi.core_yoy':[{'start':'2020-01-01','end':'2020-02-01'}]}})
        if snapshot!='none':
            run=history.start('v8-old',AS_OF,V8_VERSION,V8_HASH,'cli',1)
            gaps={} if snapshot=='empty' else {
                'cn.cpi.core_yoy':[{'start':'2021-01-01','end':'2021-02-01'}],
                'cn.rate.dr007':[{'start':'2021-01-01','end':'2021-02-01'}],
                'cn.rate.govbond_10y':[{'start':'2021-01-01','end':'2021-02-01'}]}
            history.finish(run,'completed',{'unresolvedGaps':gaps})
        expected={} if snapshot=='empty' else {'cn.cpi.core_yoy':((date(2020 if snapshot=='none' else 2021,1,1),date(2020 if snapshot=='none' else 2021,2,1)),)}
        if snapshot=='gaps':
            expected['cn.rate.govbond_10y']=((date(2021,1,1),date(2021,2,1)),)
        assert history.unresolved_gaps(active.catalog_version,active.catalog_hash)==expected
        explicit=history.start('v9-current',AS_OF,active.catalog_version,active.catalog_hash,'cli',1)
        history.finish(explicit,'completed',{'unresolvedGaps':{}})
        assert history.unresolved_gaps(active.catalog_version,active.catalog_hash)=={}
    finally:
        engine.dispose()
