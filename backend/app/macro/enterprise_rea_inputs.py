"""只读消费已存REA字段证据；不发HTTP、不从Raw补建Core、不覆盖财报。"""
from dataclasses import asdict, replace
from datetime import UTC, date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models.enterprise_rea_inputs import ReaInputAttempt, ReaInputResponse, ReaInputSupplement
from app.macro.enterprise import ReaFieldInput, _hash, _previous
from app.macro.rea_input_preview import SOURCES, _select_rea_source, rea_input_request
from app.macro.rea_input_store import ReaInputStore


def load_enterprise_rea_inputs(factory, inputs, as_of, policy, *, page_size, timezone):
    from app.macro.jobs.rea_inputs_runtime import MODELS, rea_input_schema
    base = replace(inputs, rea_fields=())
    problems = rea_input_schema(factory)
    if set(problems) == {model.__table__.fullname for model in MODELS}:
        return base
    if problems:
        raise ValueError('REA_SUPPLEMENT_SCHEMA_REQUIRED')
    if len({r.source_key for r in inputs.financials}) != len(inputs.financials):
        raise ValueError('REA_EVIDENCE_INVALID')
    needed = set()
    current = date(as_of.year, ((as_of.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    for _ in range(policy.max_quarters):
        needed.update((current, current.replace(year=current.year - 1)))
        current = _previous(current)
    cash_periods = needed | {_previous(p) for p in needed if p.month != 3}
    if inputs.renewal_estimation_enabled:
        from app.macro.renewal_estimation import candidate_periods
        cash_periods |= {date.fromisoformat(p) for target in needed for p in candidate_periods(target)}
    grouped = {}
    for row in inputs.financials:
        if row.symbol in inputs.symbols and row.end_date in cash_periods:
            grouped.setdefault((row.table, row.symbol, row.end_date), []).append(row)
    selected = {key: _select_rea_source(tuple(rows), as_of) for key, rows in grouped.items()}
    securities = {s.symbol: s for s in inputs.securities}
    requests = {}
    for (table, symbol, period), (row, _) in selected.items():
        if table not in SOURCES or period not in (needed if table == 'balance' else cash_periods):
            continue
        related = [selected.get((kind, symbol, period), (None, None)) for kind in ('income', 'balance', 'cashflow')]
        types = {r.comp_type for r, _ in related if r is not None}
        security = securities.get(symbol)
        roots = [i.root_code for i in inputs.industries if i.symbol == symbol and i.effective_from <= period
                 and (i.effective_to is None or i.effective_to >= period)]
        if (row is None or any(error == 'revision-conflict' for _, error in related)
                or types != {'1'} or not security or not security.list_date or security.list_date > period
                or len(roots) != 1 or roots[0] in (None, '801780.SI', '801790.SI')):
            continue
        try:
            request = rea_input_request(row, inputs.universe_version)
        except ValueError:
            # 无缺key/不具完整旧见证的记录不能借旧证据覆盖或猜测。
            continue
        requests[request['requestKey']] = (request, row)
    keys = sorted(requests)
    evidence = []
    zone = ZoneInfo(timezone)
    store = ReaInputStore(factory)
    with factory() as session, store.validation_pass():
        for offset in range(0, len(keys), 500):
            stored = session.scalars(select(ReaInputSupplement).where(
                ReaInputSupplement.request_key.in_(keys[offset:offset + 500])).order_by(ReaInputSupplement.request_key))
            for result in stored:
                request, source = requests[result.request_key]
                outcome = store.recover(request, source, page_size, persist=False)
                response = session.get(ReaInputResponse, result.request_key)
                attempt = session.get(ReaInputAttempt, result.request_key)
                if outcome is None or response is None or attempt is None or attempt.finished_at is None:
                    raise ValueError('REA_EVIDENCE_INVALID')
                def aware(value):
                    return value.replace(tzinfo=UTC) if value.utcoffset() is None else value
                available = max(aware(value) for value in (response.captured_at, result.created_at, attempt.finished_at))
                identity = {'requestKey': result.request_key, 'responseHash': response.response_hash,
                            'witnessVersion': result.witness_version, 'availableAt': available.isoformat()}
                for field in request['missingFields']:
                    value = outcome['fields'][field] if outcome['status'] == 'matched' else {
                        'status': outcome['status'], 'value': None}
                    evidence.append(ReaFieldInput(source.source_key, _hash(asdict(source)), field,
                        value['status'], value['value'], _hash({**identity, 'field': field, **value}),
                        available.astimezone(zone).date(), result.request_key))
    return replace(base, rea_fields=tuple(evidence))
