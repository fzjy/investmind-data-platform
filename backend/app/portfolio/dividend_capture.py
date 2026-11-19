"""独立分红字段Raw补采；一次冻结目标，持久预留先于物理请求。"""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from uuid import uuid4

from sqlalchemy import select, text

from app.config_paths import CONFIG_ROOT
from app.connectors.tushare.contracts import TushareResponse, request_hash
from app.db.models import IngestionBatch, JobCursor, PublishedSnapshot, SourcePayload
from app.ingestion.repositories import IngestionRepository, content_hash
from app.jobs.locks import job_lock_for_engine
from app.portfolio.dividend_fields import DIVIDEND_FIELDS

JOB_NAME = 'portfolio-dividend-capture'
DATASET = 'portfolio_dividend_evidence'
ROW_CAP = 2000  # 供应商合同上限，不是可放宽的本地预算。
CONFIG_FILE = CONFIG_ROOT / "portfolio/portfolio.json"


def capture_config(config=None):
    value = config if config is not None else json.loads(CONFIG_FILE.read_text(encoding='utf-8')).get('dividendEvidence')
    if not isinstance(value, dict) or type(value.get('maxHttpPerRun')) is not int or not 1 <= value['maxHttpPerRun'] <= 10000:
        raise ValueError('invalid dividend capture budget')
    timeout = value.get('timeoutSeconds')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not Decimal(str(timeout)).is_finite() or not 0 < timeout <= 120:
        raise ValueError('invalid dividend capture timeout')
    return {'maxHttpPerRun':value['maxHttpPerRun'], 'timeoutSeconds':timeout}


def _cursor(session, key, *, job=JOB_NAME):
    return session.scalar(select(JobCursor).where(JobCursor.job_name == job, JobCursor.cursor_key == key))


def _put(session, key, value, *, job=JOB_NAME):
    row = _cursor(session, key, job=job)
    if row is None:
        session.add(JobCursor(job_name=job, cursor_key=key, cursor_value=value))
    else:
        row.cursor_value = value
        row.updated_at = datetime.now(timezone.utc)


def _base(session):
    row = session.get(PublishedSnapshot, 'screener')
    if row is None:
        raise ValueError('SCREENER_BASE_REQUIRED')
    symbols = [r.get('symbol') for r in row.payload.get('stocks', [])]
    if not symbols or any(not isinstance(s, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', s) for s in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError('SCREENER_SCOPE_INVALID')
    day = (row.as_of.replace(tzinfo=timezone.utc) if row.as_of.tzinfo is None else row.as_of).astimezone(timezone.utc).date()
    return {'baseVersion':row.version, 'asOf':day.isoformat(), 'symbols':sorted(symbols),
        'apiName':'dividend', 'fields':list(DIVIDEND_FIELDS)}


def validate_capture_response(response, symbol, fields):
    if len(set(response.fields)) != len(response.fields) or not set(fields) <= set(response.fields):
        raise ValueError('RESPONSE_FIELDS_MISMATCH')
    if response.has_more or len(response.items) >= ROW_CAP:
        raise ValueError('RESPONSE_CAP_REACHED')
    for row in response.rows():
        if row.get('ts_code') != symbol:
            raise ValueError('RESPONSE_SYMBOL_MISMATCH')
        for field, value in row.items():
            if value in (None, ''):
                continue
            if field.endswith('_date') or field in {'div_listdate'}:
                if not isinstance(value, str) or not re.fullmatch(r'\d{8}', value):
                    raise ValueError('INVALID_RESPONSE_DATE')
                date.fromisoformat(value)
            if field in {'cash_div', 'cash_div_tax', 'stk_div', 'stk_bo_rate', 'stk_co_rate', 'base_share'}:
                try:
                    number = Decimal(str(value))
                except InvalidOperation:
                    raise ValueError('INVALID_RESPONSE_NUMBER') from None
                if isinstance(value, bool) or not number.is_finite() or number < 0:
                    raise ValueError('INVALID_RESPONSE_NUMBER')


def _request(api, symbol):
    return {} if api == 'bse_mapping' else {'ts_code': symbol}


def _validate_response(response, symbol, fields, api):
    if api == 'bse_mapping':
        from app.portfolio.security_mapping import validate_mapping_response
        validate_mapping_response(response, fields)
    else:
        validate_capture_response(response, symbol, fields)


def _valid_saved(session, unit, symbol, fields, *, api='dividend'):
    source = session.get(SourcePayload, unit.get('rawId'))
    batch = session.get(IngestionBatch, unit.get('batchId'))
    expected = request_hash(api, _request(api, symbol), fields)
    if unit.get('symbol') != symbol or unit.get('requestHash') != expected or unit.get('attemptObserved') is not True:
        return False
    if source is None or batch is None or batch.dataset != DATASET or batch.status != 'completed' or source.batch_id != batch.id:
        return False
    if batch.source != 'tushare' or batch.request_count != 1:
        return False
    if source.api_name != api or source.request_hash != expected or source.content_hash != content_hash(source.payload):
        return False
    if source.payload.get('_http', {}).get('credentialsRedacted'):
        return False
    if unit.get('responseContentHash') != source.content_hash:
        return False
    try:
        response = TushareResponse.model_validate(source.payload)
        if unit.get('responseRowCount') != len(response.items) or batch.row_count != len(response.items) or batch.rejected_count != 0:
            return False
        _validate_response(response, symbol, fields, api)
        return True
    except (ValueError, TypeError):
        return False


def capture_evidence_view(session, base, symbols, as_of):
    """仅把本命名空间冻结且校验成功的原件作为候选；不替换Core事实。"""
    from app.portfolio.dividend_contract import resolve_dividend_events
    from app.portfolio.dividend_report import _capital_values_complete
    report = {'status':'not_captured', 'completedCount':0, 'stocks':[],
        'capitalWindowVerified':False, 'historyCoverage':'not_proven', 'economicsReady':False}
    control = _cursor(session, 'control')
    if control is None:
        return report
    frozen = control.cursor_value
    if frozen.get('baseVersion') != base.version or frozen.get('asOf') != as_of.isoformat() or frozen.get('symbols') != symbols or frozen.get('fields') != list(DIVIDEND_FIELDS):
        return {**report, 'status':'base_changed'}
    expected_identity = content_hash({k:frozen[k] for k in ('baseVersion', 'asOf', 'symbols', 'apiName', 'fields')})
    if frozen.get('identity') != expected_identity:
        return {**report, 'status':'invalid_identity'}
    for symbol in symbols:
        row = _cursor(session, f"{frozen['identity']}:unit:{symbol}")
        item = {'symbol':symbol, 'status':'pending', 'cashEventCount':0, 'capitalFieldsCompleteEvents':0}
        if row is not None:
            unit = row.cursor_value
            item.update(status=unit['status'], rawId=unit.get('rawId'), errorType=unit.get('errorType'))
            if unit['status'] == 'completed':
                if not _valid_saved(session, unit, symbol, frozen['fields']):
                    item['status'] = 'invalid_evidence'
                else:
                    raw = session.get(SourcePayload, unit['rawId'])
                    response = TushareResponse.model_validate(raw.payload)
                    values = [{**r, 'sourceId':f'raw.source_payload:{raw.id}:row:{i}'} for i, r in enumerate(response.rows(), 1)]
                    sources = {r['sourceId']:r for r in values}
                    contract = resolve_dividend_events(values, as_of)
                    item.update(cashEventCount=len(contract['events']), events=contract['events'], issues=contract['issues'],
                        capturedAt=raw.captured_at.isoformat(), contentHash=raw.content_hash, responseRowCount=len(values),
                        fieldValueCoverage={f:sum(r.get(f) not in (None, '') for r in values) for f in frozen['fields']},
                        capitalFieldsCompleteEvents=sum(any(_capital_values_complete(sources[s]) for s in e['sourceIds']) for e in contract['events']))
                    report['completedCount'] += 1
        report['stocks'].append(item)
    report['status'] = 'completed' if report['completedCount'] == len(symbols) else 'partial'
    return report


def capture_dividend_evidence(factory, client=None, *, execute=False, config=None,
                              job=JOB_NAME, control_key='control', base_reader=_base, clock=None):
    # 一次性默认合同不变；日常调用使用独立namespace及已冻结目标读取器。
    def cursor(session, key):
        return _cursor(session, key, job=job)
    def put(session, key, value):
        return _put(session, key, value, job=job)
    settings = capture_config(config)
    engine = factory.kw['bind']
    lock = job_lock_for_engine(engine)
    acquired = execute and lock.try_acquire(job)
    report = {'status':'blocked', 'requestCount':0, 'reservedThisRun':0, 'promotedProducts':[], 'rawOnly':True}
    if execute and not acquired:
        return {**report, 'code':'CAPTURE_LOCK_BUSY'}
    try:
        with factory() as s:
            s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            try:
                proposed = base_reader(s)
            except ValueError as exc:
                return {**report, 'code':str(exc)}
            control = cursor(s, control_key)
            frozen = deepcopy(control.cursor_value) if control is not None else proposed
            if any(frozen.get(k) != v for k, v in proposed.items()):
                return {**report, 'code':'CAPTURE_BASE_CHANGED'}
            identity = content_hash(proposed)
            if control is not None and frozen.get('identity') != identity:
                return {**report, 'code':'CAPTURE_IDENTITY_INVALID'}
            units = {}
            for symbol in frozen['symbols']:
                row = cursor(s, f'{identity}:unit:{symbol}')
                if row is None:
                    continue
                unit = deepcopy(row.cursor_value)
                if unit.get('status') != 'completed':
                    return {**report, 'code':'UNRESOLVED_CAPTURE_UNIT', 'symbol':symbol, 'unitStatus':unit.get('status')}
                if not _valid_saved(s, unit, symbol, frozen['fields'], api=frozen['apiName']):
                    return {**report, 'code':'CAPTURE_EVIDENCE_INVALID', 'symbol':symbol}
                units[symbol] = unit
        report.update(identity=identity, asOf=frozen['asOf'], baseVersion=frozen['baseVersion'], symbolCount=len(frozen['symbols']),
            completedCount=len(units), pendingCount=len(frozen['symbols'])-len(units), maxHttpPerRun=settings['maxHttpPerRun'])
        if not execute:
            return {**report, 'status':'planned', 'dryRun':True}
        if len(units) == len(frozen['symbols']):
            return {**report, 'status':'completed'}
        if client is None:
            return {**report, 'code':'CAPTURE_CLIENT_REQUIRED'}
        if control is None:
            with factory.begin() as s:
                if base_reader(s) != proposed:
                    return {**report, 'code':'CAPTURE_BASE_CHANGED'}
                put(s, control_key, {**frozen, 'identity':identity})
        for symbol in frozen['symbols']:
            if symbol in units:
                continue
            if report['reservedThisRun'] >= settings['maxHttpPerRun']:
                break
            key = f'{identity}:unit:{symbol}'
            batch_id = str(uuid4())
            with factory.begin() as s:
                if base_reader(s) != proposed:
                    return {**report, 'code':'CAPTURE_BASE_CHANGED'}
                if cursor(s, key) is not None:
                    return {**report, 'code':'CAPTURE_UNIT_ALREADY_RESERVED'}
                s.add(IngestionBatch(id=batch_id, source='tushare', dataset=DATASET, mode='manual',
                    status='reserved', request_count=1, row_count=0, rejected_count=0))
                put(s, key, {'status':'reserved', 'batchId':batch_id, 'symbol':symbol,
                    'requestHash':request_hash(frozen['apiName'], _request(frozen['apiName'], symbol), frozen['fields']), 'attemptObserved':False})
            report['reservedThisRun'] += 1
            observed = False
            transport_raw_id = None
            def observe():
                nonlocal observed
                if observed:
                    raise ValueError('PHYSICAL_RETRY_FORBIDDEN')
                with factory.begin() as s:
                    if base_reader(s) != proposed:
                        raise ValueError('CAPTURE_BASE_CHANGED')
                    unit = deepcopy(cursor(s, key).cursor_value)
                    if unit['status'] != 'reserved' or unit['attemptObserved']:
                        raise ValueError('PHYSICAL_ATTEMPT_ALREADY_RECORDED')
                    unit.update(attemptObserved=True, startedAt=(clock() if clock else datetime.now(timezone.utc)).isoformat())
                    put(s, key, unit)
                observed = True
                report['requestCount'] += 1
            client.set_before_request(observe)
            def retain_http(evidence):
                nonlocal transport_raw_id
                if not observed or transport_raw_id is not None:
                    raise ValueError('UNEXPECTED_HTTP_RESPONSE')
                with factory.begin() as s:
                    payload = {'_http':evidence}
                    raw = SourcePayload(batch_id=batch_id, api_name=frozen['apiName'],
                        request_hash=request_hash(frozen['apiName'], _request(frozen['apiName'], symbol), frozen['fields']),
                        content_hash=content_hash(payload), payload=payload)
                    s.add(raw);s.flush()
                    unit = deepcopy(cursor(s, key).cursor_value)
                    unit.update(transportRawId=raw.id)
                    put(s, key, unit)
                    transport_raw_id = raw.id
            if hasattr(client, 'set_response_observer'):
                client.set_response_observer(retain_http)
            response = None
            error = None
            try:
                response = client.call(frozen['apiName'], _request(frozen['apiName'], symbol), frozen['fields'])
                if not observed:
                    raise ValueError('PHYSICAL_ATTEMPT_NOT_OBSERVED')
                _validate_response(response, symbol, frozen['fields'], frozen['apiName'])
            except Exception as exc:
                error = type(exc).__name__  # 不保存可能带凭据或供应商正文的message。
            with factory.begin() as s:
                batch = s.get(IngestionBatch, batch_id)
                raw = s.get(SourcePayload, transport_raw_id) if transport_raw_id is not None else None
                if response is not None:
                    decoded = {'fields':list(response.fields), 'items':[list(item) for item in response.items]}
                    if response.has_more is not None:
                        decoded['has_more'] = response.has_more
                    if raw is not None and raw.payload.get('_http', {}).get('credentialsRedacted'):
                        decoded = None
                        error = 'CredentialEchoInResponse'
                    # 非有限JSON数不能写PG JSON；保留原始HTTP字节证据，不伪造可用解码块。
                    try:
                        json.dumps(decoded, allow_nan=False)
                    except (ValueError, TypeError):
                        decoded = None
                        error = error or 'NonFiniteDecodedPayload'
                    if decoded is not None:
                        if raw is None:
                            raw = IngestionRepository(s).save_payload(batch, frozen['apiName'], _request(frozen['apiName'], symbol), frozen['fields'], response)
                        else:
                            raw.payload = {**raw.payload, **decoded}
                            raw.content_hash = content_hash(raw.payload)
                unit = deepcopy(cursor(s, key).cursor_value)
                finished_at = clock() if clock is not None else datetime.now(timezone.utc)
                unit.update(status='failed' if error else 'completed', rawId=raw.id if raw is not None else None,
                    errorType=error, responseRowCount=len(response.items) if response is not None else None,
                    responseContentHash=raw.content_hash if raw is not None else None,completedAt=finished_at.isoformat())
                put(s, key, unit)
                batch.status = unit['status']
                batch.row_count = len(response.items) if response is not None else 0
                batch.rejected_count = 1 if error else 0
                batch.finished_at = finished_at
                if observed and frozen['apiName'] == 'dividend':
                    from app.acquisition.contracts import StateKey
                    from app.acquisition.tracking import record_terminal
                    record_terminal(s,StateKey('stock-dividend','','security',symbol),
                        started_at=datetime.fromisoformat(unit['startedAt']),checked_at=finished_at,status='failed' if error else 'success',
                        code='DIVIDEND_RESPONSE_FAILED' if error else None,scope={'source':'tushare','api':'dividend',
                            'params':_request('dividend',symbol),'confirmedEmpty':not error and not response.items},
                        references=({'batchId':batch.id},))
            if error:
                return {**report, 'code':'CAPTURE_RESPONSE_FAILED', 'symbol':symbol, 'errorType':error}
            report['completedCount'] += 1
            report['pendingCount'] -= 1
        return {**report, 'status':'completed' if report['pendingCount'] == 0 else 'pending'}
    finally:
        if acquired:
            lock.release(job)


def run_dividend_capture(settings, factory, *, execute=False):
    from app.portfolio.routine import refresh_dividends
    config = capture_config()
    now = datetime.now(timezone.utc)
    if not execute:
        return refresh_dividends(factory, now=now, config=config)
    if settings.tushare_token is None:
        return {'status':'blocked', 'code':'TUSHARE_TOKEN_REQUIRED', 'requestCount':0}
    from app.connectors.tushare.client import TushareClient
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    rate = settings.tushare_vip_requests_per_minute if settings.tushare_mode == 'vip' else settings.tushare_standard_requests_per_minute
    with TushareClient(settings.tushare_token.get_secret_value(), max_retries=0,
        timeout_seconds=config['timeoutSeconds'], rate_limiter=RequestRateLimiter(rate)) as client:
        return refresh_dividends(factory, client, now=now, execute=True, config=config)
