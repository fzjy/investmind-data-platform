from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import select

from app.acquisition.adapters.macro import read_macro_evidence
from app.acquisition.adapters.stocks import bind_stock_attempt
from app.acquisition.catalog import load_bindings
from app.acquisition.contracts import Attempt, Evidence, EvidenceRecord, StateKey, utc
from app.acquisition.repository import StateRepository
from app.connectors.tushare.contracts import TushareResponse, request_hash
from app.connectors.tushare.strategies import Dataset, RequestPlan
from app.db.models import IngestionBatch, JobCursor, SourcePayload
from app.ingestion.repositories import content_hash
from app.stocks.update.repository import JOB_NAME


@dataclass(frozen=True)
class InitializationPage:
    records: tuple[EvidenceRecord,...]
    scanned: int
    next_cursor: str | None


def _stock_record(session, row, manifest, now):
    identity, sep, unit = row.cursor_key.partition(':')
    plan = manifest(identity).get(unit)
    value = row.cursor_value
    if not sep or not plan or not isinstance(value,dict) or not value.get('batchId'):
        return None
    batch = session.get(IngestionBatch,value['batchId'])
    if not batch or batch.dataset != plan.dataset.value or batch.status != value.get('status'):
        return None
    start, checked = utc(batch.started_at), utc(batch.finished_at)
    if not start or not checked or not start <= checked <= now:
        return None
    raw = session.scalar(select(SourcePayload).where(SourcePayload.batch_id == batch.id,
        SourcePayload.api_name == plan.api_name,SourcePayload.request_hash == request_hash(plan.api_name,plan.params,plan.fields)))
    if not raw or raw.content_hash != content_hash(raw.payload) or value.get('cachedSourcePayloadId'):
        return None
    attempts = bind_stock_attempt(plan,identity=identity,attempt_id=batch.id,started_at=start,scope_kind='routine')
    if not attempts or batch.request_count < 1:
        return None
    attempt = attempts[0]
    if batch.status in ('failed','incomplete') and batch.rejected_count:
        state = 'failed'
    elif batch.status == 'completed' and not batch.rejected_count:
        response = TushareResponse.model_validate(raw.payload)
        if (len(response.items) != value.get('received') or response.has_more or
            len(response.items) >= plan.page_size and response.has_more is not False or
            not set(plan.fields) <= set(response.fields) or
            any(r.get('ts_code',r.get('exchange')) != attempt.key.subject_key for r in response.rows())):
            return None
        state = 'success'
    else:
        return None
    scope = dict(attempt.scope)
    if state == 'success' and value.get('received') == 0 and plan.api_name == 'dividend':
        scope['confirmedEmpty'] = True
    return EvidenceRecord(attempt,Evidence(state,checked,checked if state == 'success' else None,
        'STOCK_RESPONSE_REJECTED' if state == 'failed' else None,'已核对冻结请求、批次及响应原件',
        scope,({'batchId':batch.id},)))


def plan_initialization(factory, *, root, now, limit, cursor):
    if not 1 <= limit <= 1000:
        raise ValueError('limit 必须在 1 至 1000 内')
    stage, _, index = (cursor or 'series:0').partition(':')
    if stage not in ('series','stock','market','import') or not index.isascii() or not index.isdigit():
        raise ValueError('无效的初始化游标')
    offset = int(index)
    with factory() as session:
        if stage == 'series':
            keys = sorted(StateKey(b.entry_id,b.item_id,'series',b.item_id or b.entry_id)
                          for b in load_bindings(root) if b.subject_kind == 'series' and b.source_adapter == 'macro')
            page = keys[offset:offset+limit]
            values = read_macro_evidence(session,page,now=now)
            records = []
            for key,proof in values.items():
                if proof.status not in ('success','failed') or not proof.checked_at:
                    continue
                marker = content_hash([key.entry_id,key.item_id,proof.checked_at.isoformat(),proof.status])
                records.append(EvidenceRecord(Attempt(key,marker,proof.checked_at,proof.scope,'initialization'),proof))
            next_cursor = f'series:{offset+limit}' if offset+limit<len(keys) else 'stock:0'
            return InitializationPage(tuple(records),len(page),next_cursor)
        if stage in ('market','import'):
            from app.acquisition.adapters.history import MARKET_JOBS, market_record, import_records
            from app.db.models import ExternalFile
            from app.market.data.catalog import MarketDataCatalog
            model = JobCursor if stage=='market' else ExternalFile
            query = select(model).where(model.id>offset).order_by(model.id).limit(limit+1)
            if stage=='market':
                query = query.where(JobCursor.job_name.in_(MARKET_JOBS))
                catalog = MarketDataCatalog.load()
            rows = session.scalars(query).all()
            records = []
            for row in rows[:limit]:
                if stage=='import':
                    records.extend(import_records(session,row,now))
                else:
                    try:
                        record = market_record(session,row,now,catalog)
                    except (KeyError,ValueError,TypeError,ArithmeticError):
                        record = None
                    if record:
                        records.append(record)
            next_cursor = f'{stage}:{rows[limit-1].id}' if len(rows)>limit else ('import:0' if stage=='market' else None)
            return InitializationPage(tuple(records),min(len(rows),limit),next_cursor)
        rows = session.scalars(select(JobCursor).where(JobCursor.job_name == JOB_NAME,JobCursor.id > offset)
                               .order_by(JobCursor.id).limit(limit+1)).all()
        @lru_cache(maxsize=8)
        def manifest(identity):
            if len(identity) != 64:
                return {}
            result = {}
            for value in session.scalars(select(JobCursor.cursor_value).where(JobCursor.job_name == JOB_NAME,
                       JobCursor.cursor_key.in_([identity+':manifest:'+s for s in ('calendar','actions','market','financial')]))):
                for p in value.get('plans',[]):
                    result[p['cursor_key']] = RequestPlan(**{**p,'dataset':Dataset(p['dataset']),'fields':tuple(p['fields'])})
            return result
        records = tuple(r for row in rows[:limit] if (r:=_stock_record(session,row,manifest,now)) is not None)
        return InitializationPage(records,min(len(rows),limit),f'stock:{rows[limit-1].id}' if len(rows)>limit else 'market:0')


def apply_initialization(session, records):
    repo = StateRepository(session)
    written = 0
    for record in records:
        current = repo.read((record.attempt.key,)).get(record.attempt.key)
        if current and current.checked_at and current.checked_at >= record.evidence.checked_at:
            continue
        if repo.begin(record.attempt):
            written += repo.finish(record.attempt,record.evidence)
    return {'written':written}
