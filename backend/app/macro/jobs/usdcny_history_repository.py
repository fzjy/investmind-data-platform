"""USD/CNY回填 raw/core 证据核验；不修改当前状态或发布快照。"""
from __future__ import annotations

from datetime import UTC, date, datetime
from dataclasses import replace
from hashlib import sha256
import json
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.connectors.macro.chinamoney_usdcny import fetch_usdcny_history
from app.connectors.macro.contracts import MacroFetchWindow, ProviderFetchResult, ProviderSchemaError
from app.connectors.macro.transport import JsonTransportResponse
from app.db.models import IngestionBatch, JobRun, MacroObservation, SourcePayload
from app.macro.jobs.macro_sync_repository import MacroObservationRepository, MacroPersistResult, _audit_envelope
from app.macro.catalog import MacroSeriesDefinition, MacroSourceDefinition


def fingerprint(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.utcoffset() is None else value.astimezone(UTC)


def _metadata_matches(row: MacroObservation, series: MacroSeriesDefinition,
                      source_url: str, captured_at: datetime) -> bool:
    return (row.source_url == source_url and row.name == series.name
        and row.region == series.region and row.frequency == series.frequency
        and row.unit == series.unit and row.seasonal_adjustment == series.seasonal_adjustment
        and (row.valid_until is None or utc(row.valid_until) > utc(captured_at)))


class _Replay:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = iter(pages)
        self.used = 0

    def post_json(self, url: str, payload: object) -> JsonTransportResponse:
        try:
            page = next(self.pages)
        except StopIteration:
            raise ProviderSchemaError('USDCNY_HISTORY_EVIDENCE_INVALID') from None
        expected = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        if payload != {} or page.get('request') != expected or not isinstance(page.get('response'), dict):
            raise ProviderSchemaError('USDCNY_HISTORY_EVIDENCE_INVALID')
        self.used += 1
        return JsonTransportResponse(page['response'], 1)


class UsdcnyHistoryRepository:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self.factory = factory
        self.observations = MacroObservationRepository(factory)

    def record_request(self, series: MacroSeriesDefinition, source: MacroSourceDefinition,
                       window: MacroFetchWindow, result: ProviderFetchResult, captured_at: datetime,
                       run_id: str, *, progress: bool = False) -> str:
        """每页提交同一未入 core 的临时审计；只能追加页面，终态后不可改写。"""
        if progress:
            result = replace(result, raw_payload={**result.raw_payload, 'historyStage': 'in_progress'})
        batch_id = self.observations.audit_request(series, source, window, result, captured_at, run_id)

        captured_at = utc(captured_at)
        with self.factory.begin() as session:
            batch = session.get(IngestionBatch, batch_id)
            raw = session.scalar(select(SourcePayload).where(SourcePayload.batch_id == batch_id))
            if raw.payload.get('data', {}).get('historyStage') != 'in_progress':
                return batch_id
            audit = {**_audit_envelope(series, result), 'request': raw.payload['request']}
            old_pages, new_pages = raw.payload['data']['pages'], audit['data']['pages']
            if (batch.row_count or batch.rejected_count or batch.status != 'failed'
                    or new_pages[:len(old_pages)] != old_pages or len(new_pages) < len(old_pages)):
                raise ValueError('USDCNY_HISTORY_AUDIT_CONFLICT')
            raw.payload, raw.content_hash, raw.captured_at = audit, fingerprint(audit), captured_at
            batch.status = 'completed' if result.status in {'success', 'no_change'} else 'failed'
            batch.request_count, batch.finished_at = result.request_count, captured_at
        return batch_id

    def start(self, details: dict[str, object]) -> str:
        run_id = str(uuid4())
        with self.factory.begin() as session:
            session.add(JobRun(id=run_id, job_name='backfill-usdcny-history',
                business_key='usdcny-history-v1', status='running', details=details))
        return run_id

    def finish(self, run_id: str, report: dict[str, object]) -> None:
        with self.factory.begin() as session:
            row = session.get(JobRun, run_id)
            if row is None:
                raise ValueError('USDCNY_HISTORY_RUN_MISSING')
            row.status = str(report['status'])
            row.request_count = int(report['requestCount'])
            row.row_count = int(report['rowCount'])
            row.details = {**row.details, **report}
            row.finished_at = datetime.now(UTC)

    def persist(self, series: MacroSeriesDefinition, result: ProviderFetchResult,
                captured_at: datetime, *, batch_id: str) -> MacroPersistResult:
        """锁内USD专用修复：同值但无效元数据建立新本地版本，旧行不覆盖。

        上游历史没有revision；repair-metadata明确为本地修复身份，不伪造供应商修订。
        良好同值版本继续走原幂等路径，core后中断也不会重复新增修复。
        """
        captured_at = utc(captured_at)
        observations = []
        with self.factory() as session:
            for observation in result.observations:
                latest = session.scalar(select(MacroObservation).where(
                    MacroObservation.series_id == series.id,
                    MacroObservation.source == observation.provider,
                    MacroObservation.observed_at == observation.observed_at,
                ).order_by(MacroObservation.released_at.desc(), MacroObservation.id.desc()))
                if (latest is not None and latest.value == observation.value
                        and not _metadata_matches(latest, series, observation.source_url, captured_at)):
                    if utc(latest.released_at) > captured_at:
                        raise ValueError('USDCNY_HISTORY_EVIDENCE_INVALID')
                    repair = fingerprint({'oldRowId': latest.id,
                        'series': series.model_dump(mode='json', by_alias=True),
                        'provider': observation.provider, 'sourceUrl': observation.source_url})
                    observation = replace(observation, revision=f'repair-metadata:{repair[:32]}')
                observations.append(observation)
        return self.observations.persist(series, replace(result, observations=tuple(observations)),
            captured_at, batch_id=batch_id)

    def confirm(self, series: MacroSeriesDefinition, source: MacroSourceDefinition,
                window: MacroFetchWindow, result: ProviderFetchResult, captured_at: datetime,
                batch_id: str) -> list[dict[str, object]]:
        captured_at = utc(captured_at)
        points: list[dict[str, object]] = []
        with self.factory() as session:
            for observation in result.observations:
                row = session.scalar(select(MacroObservation).where(
                    MacroObservation.series_id == series.id,
                    MacroObservation.source == source.provider,
                    MacroObservation.observed_at == observation.observed_at,
                    MacroObservation.released_at <= captured_at,
                ).order_by(MacroObservation.released_at.desc(), MacroObservation.id.desc()))
                if row is None or row.value != observation.value:
                    raise ValueError('USDCNY_HISTORY_EVIDENCE_INVALID')
                points.append({'observed_at': row.observed_at.isoformat(),
                    'value': str(observation.value), 'revision': row.revision,
                    'released_at': utc(row.released_at).isoformat()})
        self.verify(series, source, window, captured_at, batch_id, points)
        return points

    def verify(self, series: MacroSeriesDefinition, source: MacroSourceDefinition,
               window: MacroFetchWindow, captured_at: datetime, batch_id: str,
               points: list[dict[str, object]]) -> None:
        """重放已存响应（无 HTTP），逐行证明 core，不只相信 done 或计数。"""
        try:
            with self.factory() as session:
                batch = session.get(IngestionBatch, batch_id)
                raws = session.scalars(select(SourcePayload).where(SourcePayload.batch_id == batch_id)).all()
                if (batch is None or batch.source != source.provider or batch.dataset != f'macro:{series.id}'
                        or batch.status != 'completed' or batch.rejected_count != 0 or len(raws) != 1):
                    raise ValueError('invalid batch')
                raw = raws[0]
                request = {'provider': source.provider, 'seriesId': series.id,
                    'source': source.model_dump(mode='json', by_alias=True),
                    'window': {'start': window.start.isoformat(), 'end': window.end.isoformat(),
                        'asOf': window.as_of.isoformat()}}
                audit = raw.payload
                if (raw.content_hash != fingerprint(audit) or raw.request_hash != fingerprint(request)
                        or raw.api_name != 'macro:chinamoney' or utc(raw.captured_at) != utc(captured_at)
                        or audit.get('request') != request or audit.get('status') != 'success'
                        or audit.get('provider') != source.provider or audit.get('seriesId') != series.id):
                    raise ValueError('invalid raw')
                pages = audit['data']['pages']
                if not isinstance(pages, list) or not all(isinstance(page, dict) for page in pages):
                    raise ValueError('invalid pages')
                replay = _Replay(pages)
                result = fetch_usdcny_history(replay, series, source, window)
                if (result.status != 'success' or replay.used != len(pages)
                        or result.request_count != batch.request_count
                        or audit.get('requestCount') != batch.request_count
                        or len(result.observations) != len(points)
                        or batch.row_count > len(points)):
                    raise ValueError('invalid response')
                expected = {o.observed_at.isoformat(): o.value for o in result.observations}
                seen: set[str] = set()
                for point in points:
                    observed = point['observed_at']
                    if observed in seen or observed not in expected:
                        raise ValueError('invalid point')
                    seen.add(observed)
                    row = session.scalar(select(MacroObservation).where(
                        MacroObservation.series_id == series.id, MacroObservation.source == source.provider,
                        MacroObservation.observed_at == date.fromisoformat(observed),
                        MacroObservation.revision == point['revision'],
                        MacroObservation.released_at == utc(datetime.fromisoformat(point['released_at']))))
                    if (row is None or row.value != expected[observed] or str(expected[observed]) != point['value']
                            or utc(row.released_at) != utc(datetime.fromisoformat(point['released_at']))
                            or utc(row.released_at) > utc(captured_at)
                            or not _metadata_matches(row, series, str(source.source_url), captured_at)):
                        raise ValueError('invalid core')
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError('USDCNY_HISTORY_EVIDENCE_INVALID') from None
