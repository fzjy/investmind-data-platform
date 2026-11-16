"""当前持久采集状态；独立于业务发布，不按日期推算成功。"""
from datetime import datetime, timezone
from sqlalchemy import select
from app.db.models.ops import JobRun, MacroSeriesState
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.audit_records import extension_entries, financial_entries, record, utc


class CollectionAuditReader:
    def __init__(self, factory, *, catalog_loader=None, extension_loader=None, enterprise_enabled=None, clock=None):
        from app.macro.extensions.config import load_extension_config
        from app.stocks.api.supply import financial_collection_enabled
        self.factory = factory
        self.catalog_loader = catalog_loader or (lambda: MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE))
        self.extension_loader = extension_loader or load_extension_config
        self.enterprise_enabled = enterprise_enabled or financial_collection_enabled
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self):
        now, catalog = utc(self.clock()), self.catalog_loader()
        from app.acquisition.macro_compat import read_compatible_macro_audit
        def legacy():
            entries = self.base_entries(now, catalog)
            entries.extend(extension_entries(self.factory, self.extension_loader(), now))
            entries.extend(financial_entries(self.factory, self.enterprise_enabled(), now))
            return {'schemaVersion':'1.0','basis':'current-collection-records','readAt':now.isoformat(),
                    'catalogVersion':catalog.catalog_version,'entries':entries}
        return read_compatible_macro_audit(self.factory, legacy, now=now)

    def base_entries(self, now, catalog):
        with self.factory() as session:
            states = {r.series_id: r for r in session.scalars(select(MacroSeriesState))}
            running = session.scalars(select(JobRun).where(JobRun.status == 'running')).all()
            entries = []
            for definition in catalog.series:
                sid, enabled = definition.id, bool(definition.sources)
                row = states.get(sid)
                checked = utc(row.last_checked_at) if row else None
                success = utc(row.last_success_at) if row else None
                error = row.last_error_code if row else None
                status = 'unrecorded'
                if checked and checked <= now and (success is None or success <= checked):
                    if error in ('MACRO_VALUE_EXPIRED', 'MACRO_HISTORY_GAP'):
                        error = 'COLLECTION_LEGACY_QUALITY_STATE'
                    elif error and error.endswith('_REQUEST_BUDGET_EXHAUSTED'):
                        error = 'COLLECTION_LEGACY_NONEXECUTION_STATE'
                    elif error:
                        status = 'failed'
                    elif success == checked and (not row.stale or row.stale_reason == 'MACRO_HISTORY_GAP'):
                        status = 'success'
                    else:
                        error = 'COLLECTION_RESULT_UNRECORDED'
                elif checked:
                    error = 'COLLECTION_RECORD_INVALID'
                active = [r for r in running if isinstance(r.details, dict)
                    and sid in r.details.get('collectionSeriesIds', [])
                    and utc(r.started_at) <= now and (checked is None or utc(r.started_at) >= checked)]
                if active:
                    status, error, checked = 'running', None, max(utc(r.started_at) for r in active)
                entries.append(record(sid, enabled, status, checked, success, error, now=now))
        return entries
