"""REA冻结计划的有界多字段执行；runtime负责正式schema门和跨进程锁。"""
from collections import Counter
from datetime import date
from uuid import uuid4

import httpx
from sqlalchemy.exc import IntegrityError
from app.connectors.macro.budget import HttpRequestBudget
from app.connectors.tushare.client import TushareClient
from app.connectors.tushare.errors import TushareError
from app.macro.rea_input_preview import SOURCES, preview_rea_inputs
from app.macro.rea_input_store import EVIDENCE_ERRORS, ReaInputStore


def read_rea_progress(factory, plan, sources, page_size):
    store = ReaInputStore(factory)
    with store.validation_pass():
        return _read_rea_progress(store, plan, sources, page_size)


def _read_rea_progress(store, plan, sources, page_size):
    pending = replay = matched = unavailable = field_matched = 0
    fields, bindings, blocked = Counter(), Counter(), Counter()
    for request in plan['requests']:
        try:
            source = sources[request['coreBinding']['sourceKey']]
            outcome = store.recover(request, source, page_size)
            if outcome is None:
                pending += 1
                continue
            if not store.result_exists(request['requestKey']):
                replay += 1
                pending += 1
            valid = outcome['status'] == 'matched' and all(v['status'] == 'matched' for v in outcome['fields'].values())
            matched += valid
            unavailable += not valid
            if outcome['status'] != 'matched':
                bindings[outcome['status']] += 1
            for value in outcome['fields'].values():
                if value['status'] == 'matched':
                    field_matched += 1
                else:
                    fields[value['status']] += 1
        except ValueError as exc:
            if str(exc) not in EVIDENCE_ERRORS:
                raise
            blocked[str(exc)] += 1
            pending += 1
    return {'processingCompleted': pending == 0, 'pendingCount': pending, 'replayPendingCount': replay,
            'matchedCount': matched, 'unavailableCount': unavailable, 'fieldMatchedCount': field_matched,
            'fieldUnavailableReasons': dict(sorted(fields.items())), 'bindingUnavailableReasons': dict(sorted(bindings.items())),
            'blockedReasons': sorted(blocked), 'unverifiedCount': sum(blocked.values())}


def prepare_rea_plan(factory, inputs, as_of, policy, quarters, request_limit, page_size, *, persist=False):
    if type(page_size) is not int or not 1 <= page_size <= 5000:
        raise ValueError('REA_PAGE_POLICY_CHANGED')
    current = preview_rea_inputs(inputs, as_of, policy, quarters, request_limit)
    store = ReaInputStore(factory)
    latest = store.latest()
    if latest:
        progress = read_rea_progress(factory, latest['plan'], latest['sources'], latest['pageSize'])
        if progress['blockedReasons']:
            raise ValueError(progress['blockedReasons'][0])
        if not progress['processingCompleted']:
            if latest['pageSize'] != page_size:
                raise ValueError('REA_PAGE_POLICY_CHANGED')
            original = preview_rea_inputs(inputs, date.fromisoformat(latest['plan']['asOfDate']), policy, quarters, request_limit)
            if original['planFingerprint'] != latest['plan']['planFingerprint']:
                raise ValueError('REA_FROZEN_PLAN_CHANGED')
            return {**latest, 'progress': progress}
        if latest['plan']['planFingerprint'] == current['planFingerprint']:
            if latest['pageSize'] != page_size:
                raise ValueError('REA_PAGE_POLICY_CHANGED')
            return {**latest, 'progress': progress}
    if persist:
        return store.freeze(current, inputs, page_size)
    sources = {row.source_key: row for row in inputs.financials}
    return {'plan': current, 'sources': {r['coreBinding']['sourceKey']: sources[r['coreBinding']['sourceKey']] for r in current['requests']},
            'pageSize': page_size}


def execute_rea_inputs_batch(factory, inputs, as_of, policy, *, quarters, request_limit, page_size,
                             token, transport=None, rate_limiter=None, probe=False, source=None):
    result = {'requestCount': 0, 'responseCount': 0, 'httpLimit': min(1, request_limit) if probe else request_limit,
              'configuredHttpLimit': request_limit, 'operation': 'probe' if probe else 'batch',
              'publicationRequested': False, 'physicalBudgetEnforced': True,
              'requestCountBasis': 'committed-reservations-conservative-attempt-upper-bound',
              **({'probeReady': False, 'probeSource': source or 'balance'} if probe else {})}
    if (source is not None and source not in SOURCES) or (source is not None and not probe):
        return {**result, 'status': 'blocked', 'errorCode': 'REA_SOURCE_INVALID'}
    try:
        prepared = prepare_rea_plan(factory, inputs, as_of, policy, quarters, request_limit, page_size, persist=True)
        plan, sources = prepared['plan'], prepared['sources']
        result.update(planFingerprint=plan['planFingerprint'], poolCount=plan['poolCount'], poolVersion=plan['poolVersion'],
                      outputPeriods=plan['outputPeriods'], frozenAsOfDate=plan['asOfDate'], logicalRequestCount=plan['logicalRequestCount'])
        progress = prepared.get('progress') or read_rea_progress(factory, plan, sources, page_size)
        if progress['blockedReasons']:
            return {**result, **progress, 'status': 'blocked', 'errorCode': progress['blockedReasons'][0]}
        budget = HttpRequestBudget(result['httpLimit'])
        store = ReaInputStore(factory)
        selected_source = source or 'balance'
        inner = transport if transport is not None else httpx.HTTPTransport(retries=0)
        error = None
        new_matched = 0
        with store.validation_pass(), TushareClient(token, transport=inner, max_retries=0, rate_limiter=rate_limiter) as client:
            for request in plan['requests']:
                original = sources[request['coreBinding']['sourceKey']]
                # 重放所有已存Raw，不因probe源过滤或当次预算耗尽而漏写Core。
                if store.recover(request, original, page_size, persist=True) is not None:
                    continue
                if budget.used >= budget.limit or (probe and request['table'] != selected_source):
                    continue
                reserved = False
                def reserve():
                    nonlocal reserved
                    store.reserve(request, plan['planFingerprint'], str(uuid4()), page_size)
                    budget.consume()
                    reserved = True
                client.set_before_request(reserve)
                try:
                    response = client.call(request['apiName'], request['params'], request['fields'])
                except TushareError as exc:
                    if reserved:
                        store.fail(request['requestKey'], type(exc).__name__)
                    error = type(exc).__name__
                    break
                store.receive(request['requestKey'], response)
                result['responseCount'] += 1
                outcome = store.recover(request, original, page_size, persist=True)
                new_matched += outcome['status'] == 'matched' and all(v['status'] == 'matched' for v in outcome['fields'].values())
        result['requestCount'] = budget.used
        progress = read_rea_progress(factory, plan, sources, page_size)
        status = ('blocked' if error or progress['blockedReasons'] else 'partial' if not progress['processingCompleted']
                  else 'completed-with-gaps' if progress['unavailableCount'] else 'completed')
        return {**result, **progress, 'status': status,
                'budgetExhausted': budget.used >= budget.limit and progress['pendingCount'] > 0,
                **({'probeReady': error is None and budget.used == result['responseCount'] == new_matched == 1} if probe else {}),
                **({'errorCode': error or progress['blockedReasons'][0]} if error or progress['blockedReasons'] else {})}
    except (ValueError, IntegrityError) as exc:
        code = str(exc) if isinstance(exc, ValueError) and str(exc) in EVIDENCE_ERRORS else 'REA_RESERVATION_CONFLICT' if isinstance(exc, IntegrityError) else 'REA_EXECUTION_INVALID'
        if 'budget' in locals():
            result['requestCount'] = budget.used
        return {**result, 'status': 'blocked', 'errorCode': code}
