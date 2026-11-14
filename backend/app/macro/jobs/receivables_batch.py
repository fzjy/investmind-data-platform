"""固定企业补充规划的单次有界执行；外层runtime持有跨进程锁。"""
from collections import Counter
from uuid import uuid4

import httpx

from app.connectors.macro.budget import HttpRequestBudget
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.errors import TushareError
from app.macro.receivables_preview import preview_receivables
from app.macro.receivables_store import EVIDENCE_ERRORS, ReceivablesStore
from app.macro.balance_supplement import RECEIVABLES, target_options


def read_receivables_progress(factory, inputs, plan: dict, page_size: int, *, tolerate_invalid: bool = False, target=RECEIVABLES) -> dict:
    store = ReceivablesStore(factory,**target_options(target))
    sources = {row.source_key: row for row in inputs.financials if row.table == 'balance'}
    counts = Counter()
    invalid = Counter()
    for request in plan['requests']:
        try:
            source = sources.get(request['coreBinding']['sourceKey'])
            if source is None:
                raise ValueError('ENTERPRISE_SOURCE_BINDING_INVALID')
            outcome = store.recover(request, source, page_size)
        except ValueError as exc:
            if not tolerate_invalid or str(exc) not in EVIDENCE_ERRORS:
                raise
            # 不将损坏目标算已完成；仍逐项核对其他已提交结果，不能退回旧统计。
            invalid[str(exc)] += 1
            counts['pending'] += 1
            continue
        counts[outcome['status'] if outcome is not None else 'pending'] += 1
    unavailable = {reason: count for reason, count in sorted(counts.items()) if reason not in ('matched', 'pending')}
    return {'processingCompleted': counts['pending'] == 0, 'matchedCount': counts['matched'],
        'unavailableCount': sum(unavailable.values()), 'pendingCount': counts['pending'],
        'unavailableReasons': unavailable, 'unverifiedCount': sum(invalid.values()),
        'blockedReasons': sorted(invalid)}


def execute_receivables_batch(factory, inputs, as_of, policy, *, quarters: int,
        request_limit: int, page_size: int, token: str, transport=None, rate_limiter=None,
        probe: bool = False, target=RECEIVABLES) -> dict:
    options=target_options(target)
    plan = preview_receivables(inputs, as_of, policy, quarters, request_limit,**options)
    if type(page_size) is not int or not 1 <= page_size <= 5000:
        raise ValueError('ENTERPRISE_PAGE_SIZE_INVALID')
    budget = HttpRequestBudget(min(request_limit, 1) if probe else request_limit)
    store = ReceivablesStore(factory,**options)
    run_id = str(uuid4())
    sources = {row.source_key: row for row in inputs.financials if row.table == 'balance'}
    result = {'runId': run_id, 'planFingerprint': plan['planFingerprint'],
        'poolCount': plan['poolCount'], 'logicalRequestCount': plan['logicalRequestCount'],
        'requestCount': 0, 'httpLimit': budget.limit, 'configuredHttpLimit': request_limit,
        'operation': 'probe' if probe else 'batch', 'publicationRequested': False}
    error = None
    response_count = 0
    new_matched_count = 0
    try:
        # 先验证全部已存证据，避免后半段损坏时先对前半段盲目发HTTP。
        read_receivables_progress(factory, inputs, plan, page_size,**options)
        inner = transport if transport is not None else httpx.HTTPTransport(retries=0)
        with TushareClient(token, transport=inner, max_retries=0, rate_limiter=rate_limiter) as client:
            for request in plan['requests']:
                source = sources[request['coreBinding']['sourceKey']]
                if store.recover(request, source, page_size, persist=True) is not None:
                    continue
                if budget.used >= budget.limit:
                    continue
                attempt_id = None
                def reserve():
                    nonlocal attempt_id
                    budget.consume()
                    attempt_id = store.reserve(request, plan['planFingerprint'], run_id, page_size)
                client.set_before_request(reserve)
                try:
                    response = client.call(request['apiName'], request['params'], request['fields'])
                except TushareError as exc:
                    if attempt_id is not None:
                        store.fail(attempt_id, type(exc).__name__)
                    error = type(exc).__name__
                    break
                store.receive(attempt_id, response)
                response_count += 1
                outcome = store.recover(request, source, page_size, persist=True)
                new_matched_count += outcome['status'] == 'matched'
        progress = read_receivables_progress(factory, inputs, plan, page_size,**options)
    except ValueError as exc:
        known = EVIDENCE_ERRORS | {'ENTERPRISE_RESERVATION_FAILED'}
        error = str(exc) if str(exc) in known else 'ENTERPRISE_EXECUTION_INVALID'
        progress = read_receivables_progress(factory, inputs, plan, page_size, tolerate_invalid=True,**options)
    status = ('blocked' if error else 'partial' if not progress['processingCompleted']
        else 'completed-with-gaps' if progress['unavailableCount'] else 'completed')
    return {**result, **progress, 'status': status, 'requestCount': budget.used,
        'responseCount': response_count, 'newMatchedCount': new_matched_count,
        'budgetExhausted': budget.used >= budget.limit and progress['pendingCount'] > 0,
        **({'probeReady': error is None and budget.used == response_count == new_matched_count == 1} if probe else {}),
        **({'errorCode': error} if error else {})}
