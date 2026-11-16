from datetime import UTC, datetime

from sqlalchemy import select

from app.acquisition.adapters.stocks import stock_attempts
from app.acquisition.contracts import Evidence
from app.acquisition.repository import StateRepository
from app.acquisition.tracking import record_failed_attempt
from app.connectors.tushare.contracts import TushareResponse, request_hash
from app.db.models import IngestionBatch, SourcePayload
from app.ingestion.repositories import content_hash


def tracked_pages(factory,plan,scope_kind,page_size,identity,runner):
    attempts = stock_attempts(plan,identity,scope_kind=scope_kind)
    if not attempts:
        return runner(plan)
    try:
        result = runner(plan)
    except Exception:
        for attempt in attempts:
            record_failed_attempt(factory,attempt,code='PAGINATED_REQUEST_FAILED',
                                  reason='分页请求或提交失败',checked_at=datetime.now(UTC),references=())
        raise
    with factory.begin() as session:
        verified = result.completed and not result.resumed_from_offset and not result.filtered_count
        for index, page in enumerate(result.batches):
            batch = session.get(IngestionBatch,page.batch_id)
            params = {**plan.params,'limit':str(page_size),'offset':str(index*page_size+result.resumed_from_offset)}
            raw = session.scalar(select(SourcePayload).filter_by(batch_id=page.batch_id,api_name=plan.api_name,
                                request_hash=request_hash(plan.api_name,params,plan.fields)))
            if not (batch and batch.status == 'completed' and raw and content_hash(raw.payload) == raw.content_hash):
                verified = False
                break
            response = TushareResponse.model_validate(raw.payload)
            subject = 'exchange' if plan.api_name == 'trade_cal' else 'ts_code'
            if (len(response.items) != page.received_count or not set(plan.fields)<=set(response.fields) or
                any(r.get(subject)!=plan.params[subject] for r in response.rows())):
                verified = False
                break
        checked = datetime.now(UTC)
        for attempt in attempts:
            repo = StateRepository(session)
            if not repo.begin(attempt):
                continue
            status = 'failed' if result.rejected_count else 'success' if verified else 'unrecorded'
            scope = {**attempt.scope,'confirmedEmpty':bool(verified and result.received_count==0 and plan.api_name=='dividend')}
            refs = tuple({'batchId':b.batch_id} for b in result.batches[-3:])
            repo.finish(attempt,Evidence(status,checked,checked if verified else None,
                        'PAGE_REJECTED' if status=='failed' else None,'分页范围已核对' if verified else '部分分页未核全',scope,refs))
    return result
