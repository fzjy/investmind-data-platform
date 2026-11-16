"""从现有指数与 ETF 候选表读取可核对的请求和业务行。"""
from sqlalchemy import select

from app.acquisition.contracts import Availability, Evidence, utc
from app.db.models.enterprise_target_evidence import TargetObservation, TargetCandidate
from app.db.models.enterprise_target_execution import TargetExecutionAttempt
from app.macro.target_evidence_contract import evidence_hash
from app.macro.target_evidence_store import _check_candidate, _decode_raw
from app.macro.target_execution_plan import step_request

APIS = {'index-basic':'index_basic','index-weights':'index_weight','etf-basic':'etf_basic'}


def target_status(session,key,now):
    api = APIS[key.entry_id]
    parameter = 'index_code' if api=='index_weight' else 'ts_code'
    value = Availability('missing',reason='该对象没有通过校验的入库候选')
    proof = None
    query = (select(TargetObservation,TargetCandidate)
             .outerjoin(TargetCandidate,TargetCandidate.observation_id==TargetObservation.observation_id)
             .where(TargetObservation.api_name==api,TargetObservation.params[parameter].as_string()==key.subject_key,
                    TargetObservation.captured_at<=now)
             .order_by(TargetObservation.captured_at.desc(),TargetCandidate.created_at.desc()).limit(50))
    for raw,candidate in session.execute(query):
        checked = utc(raw.captured_at)
        scope = {'source':'tushare','api':api,'params':raw.params}
        refs = ({'observationId':raw.observation_id},)
        try:
            decoded = _decode_raw(raw)
            if candidate is None:
                raise ValueError
            _check_candidate(candidate,{raw.observation_id:decoded})
            result = candidate.payload
            status = result['qualityStatus']
            if status == 'rejected':
                item = Evidence('failed',checked,None,'TARGET_RESPONSE_REJECTED','候选行未通过校验',scope,refs)
            elif status == 'valid_candidate' and (result.get('metadata') or result.get('snapshotCandidates')):
                if value.kind!='available':
                    value = Availability('available',reason='已有通过校验的原始候选',source='tushare')
                if api=='index_weight' and result.get('membershipReady') is not True:
                    item = Evidence('unrecorded',checked,reason='已取得权重候选，来源未证明覆盖全部成分',scope=scope,references=refs)
                else:
                    item = Evidence('success',checked,checked,reason='已核对该对象的请求、原件和候选',scope=scope,references=refs)
            else:
                item = Evidence('unrecorded',checked,reason='来源未提供可用候选',scope=scope,references=refs)
        except (KeyError,TypeError,ValueError):
            item = Evidence('unrecorded',checked,reason='尚无完整的原件及规范化凭证',scope=scope)
        if proof is None:
            proof = item
        if value.kind=='available':
            break
    step = TargetExecutionAttempt.request['request']
    failed = session.scalar(select(TargetExecutionAttempt).where(TargetExecutionAttempt.state=='failed',
        step['apiName'].as_string()==api,step['params'][parameter].as_string()==key.subject_key,
        TargetExecutionAttempt.finished_at<=now).order_by(TargetExecutionAttempt.finished_at.desc()).limit(1))
    if failed and failed.request_hash==evidence_hash(failed.request):
        req = step_request(failed.request)
        checked = utc(failed.finished_at)
        if utc(failed.created_at)<=checked and (not proof or not proof.checked_at or checked>=proof.checked_at):
            proof = Evidence('failed',checked,error_code='TARGET_REQUEST_FAILED',reason='该对象最近的请求失败',
                scope={'source':'tushare','api':api,'params':dict(req.params)},references=({'runId':failed.run_id},))
    if key.entry_id=='index-basic':
        from app.portfolio.reference_catalog import TARGETS, JOB, IDENTITY, FIELDS
        if key.subject_key in TARGETS:
            from app.portfolio.reference_history import _saved, _cursor
            from app.portfolio.dividend_ledger import _original_rows
            from app.connectors.tushare.contracts import request_hash
            from app.db.models import IngestionBatch
            symbol = key.subject_key
            unit = _cursor(session,JOB,IDENTITY+':'+symbol)
            if unit:
                try:
                    raw = _saved(session,unit,'index_basic',JOB)
                    params = {'market':'CSI','symbol':symbol}
                    rows = _original_rows(raw,list(FIELDS))
                    checked = utc(session.get(IngestionBatch,raw.batch_id).finished_at)
                    if (checked and checked<=now and raw.request_hash==request_hash('index_basic',params,FIELDS)
                            and all(str(r.get('ts_code','')).split('.')[0]==symbol for r in rows)):
                        value = Availability('available' if rows else 'missing',reason='已核对指数目录查询原件',source='tushare')
                        if not proof or checked>proof.checked_at:
                            proof = Evidence('success',checked,checked,reason='指数目录请求已完成',
                                scope={'source':'tushare','api':'index_basic','params':params},references=({'batchId':raw.batch_id},))
                except (ValueError,TypeError,KeyError):
                    pass
    return proof,value
