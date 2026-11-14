from dataclasses import asdict
from datetime import date, datetime

from sqlalchemy import select

from app.db.models import JobCursor, PublishedSnapshot
from app.ingestion.repositories import IngestionRepository
from app.market.data.contracts import MarketTarget
from app.market.data.catalog import resolve_market_targets
from app.market.data.repository import MarketQuoteRepository
from app.market.data.scheduling import most_recent_market_schedule, next_market_schedule

SCHEDULE_JOB = 'market-schedule'


def _key(catalog, slot):
    return f'{catalog.fingerprint}:{slot.isoformat()}'


class MarketScheduleRepository:
    def __init__(self, factory):
        self.factory = factory

    def inspect(self, catalog, *, now: datetime) -> dict:
        slot = most_recent_market_schedule(now, catalog.schedule)
        base = {'latestDueAt': slot.isoformat(), 'nextRunAt': next_market_schedule(now, catalog.schedule).isoformat(),
            'lastCollectedAt': None, 'lastPublishedAt': None, 'collected': False, 'published': False, 'targets': [], 'plannedAt': None}
        with self.factory() as session:
            record = session.scalar(select(JobCursor).where(JobCursor.job_name == SCHEDULE_JOB, JobCursor.cursor_key == _key(catalog, slot)))
            if record is None:
                return base
            proof = record.cursor_value
            targets = self.targets(catalog, proof.get('targets', []))
            if len(targets) != 15:
                return base
            try:
                planned_at = datetime.fromisoformat(proof['plannedAt'])
                if most_recent_market_schedule(planned_at,catalog.schedule) != slot:
                    return base
                resolve_market_targets(catalog,now,targets,planned_at=planned_at)
            except (ValueError,TypeError,KeyError):
                return base
            base['targets'] = proof['targets']
            base['plannedAt'] = proof['plannedAt']
            if not all(MarketQuoteRepository(self.factory).is_complete(t, checked_at=now) for t in targets):
                return base
            version = proof.get('publishedVersion')
            pointers = session.scalars(select(PublishedSnapshot).where(PublishedSnapshot.product.in_(('market-overview','market-explorer')))).all()
            published = bool(version and len(pointers) == 2 and all(p.version == version for p in pointers))
            return {**base, 'collected': True, 'published': published,
                'lastCollectedAt': proof.get('collectedAt'), 'lastPublishedAt': proof.get('publishedAt') if published else None,
                'targets': proof['targets']}

    @staticmethod
    def targets(catalog, records):
        assets = {a.asset_code: a for a in catalog.assets}
        try:
            result = tuple(MarketTarget(assets[r['assetCode']], date.fromisoformat(r['startDate']),
                date.fromisoformat(r['endDate']), r['identity'], catalog.sync.pct_change_tolerance_percent_points) for r in records)
            return result if len({t.asset.asset_code for t in result}) == 15 else ()
        except (KeyError, ValueError, TypeError):
            return ()

    def begin_attempt(self, catalog, *, slot, targets, checked_at):
        records = [{'assetCode':t.asset.asset_code,'startDate':t.start_date.isoformat(),
            'endDate':t.end_date.isoformat(),'identity':t.identity} for t in targets]
        with self.factory.begin() as session:
            current = session.scalar(select(JobCursor).where(JobCursor.job_name==SCHEDULE_JOB,JobCursor.cursor_key==_key(catalog,slot)))
            if current is None:
                session.add(JobCursor(job_name=SCHEDULE_JOB,cursor_key=_key(catalog,slot),
                    cursor_value={'targets':records,'plannedAt':checked_at.isoformat()},updated_at=checked_at))

    def record(self, catalog, *, slot, targets, checked_at, collection, publication):
        if collection.get('status') != 'completed':
            return
        if len(targets) != 15 or not all(MarketQuoteRepository(self.factory).is_complete(t, checked_at=checked_at) for t in targets):
            return
        records = [{'assetCode': t.asset.asset_code, 'startDate': t.start_date.isoformat(),
            'endDate': t.end_date.isoformat(), 'identity': t.identity} for t in targets]
        proof = {'collectedAt': checked_at.isoformat(), 'targets': records}
        if publication and publication.get('status') in {'completed','no_change'}:
            proof.update(publishedAt=checked_at.isoformat(), publishedVersion=publication['version'])
        with self.factory.begin() as session:
            existing = session.scalar(select(JobCursor).where(JobCursor.job_name==SCHEDULE_JOB,JobCursor.cursor_key==_key(catalog,slot)))
            proof['plannedAt'] = existing.cursor_value.get('plannedAt',checked_at.isoformat()) if existing else checked_at.isoformat()
            IngestionRepository(session).upsert(JobCursor, {'job_name': SCHEDULE_JOB, 'cursor_key': _key(catalog, slot)},
                {'cursor_value': proof, 'updated_at': checked_at})
