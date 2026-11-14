"""只读复核既存补充结果；不采集、不写重放、不覆盖原财报字段。"""
from dataclasses import asdict, replace
from datetime import UTC, date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models.enterprise_receivables import ReceivableAttempt, ReceivableResponse, ReceivableSupplement
from app.macro.enterprise import ReceivableInput, _hash, _previous, _select
from app.macro.receivables_preview import receivable_request
from app.macro.receivables_store import ReceivablesStore
from app.macro.balance_supplement import RECEIVABLES, target_options


def load_enterprise_receivables(factory, inputs, as_of, policy, *, page_size, timezone, target=RECEIVABLES):
    from app.macro.jobs.receivables_runtime import _schema
    options=target_options(target)
    Attempt,Response,Supplement=target.models
    destination='receivables' if target is RECEIVABLES else 'contract_liabilities'
    missing = _schema(factory,**options)
    names = {model.__table__.fullname for model in target.models}
    if set(missing) == names:
        return inputs
    if missing:
        raise ValueError('ENTERPRISE_SUPPLEMENT_SCHEMA_REQUIRED')
    # 消费窗口由自身展示政策决定；不能因利润或存货基数不合格清空合法应收。
    # 最多max_quarters+4个余额期，仍只消费已存证据，不扩采集8期或HTTP预算。
    base = replace(inputs, **{destination:None})
    needed=set()
    period=date(as_of.year,((as_of.month-1)//3)*3+1,1)-timedelta(days=1)
    for _ in range(policy.max_quarters):
        needed.update((period,date(period.year-1,period.month,period.day)))
        period=_previous(period)
    grouped={}
    for row in base.financials:
        if row.table=='balance' and row.symbol in base.symbols and row.end_date in needed:
            grouped.setdefault((row.symbol,row.end_date),[]).append(row)
    requests={}
    for rows in grouped.values():
        row,_=_select(tuple(rows),as_of)
        if row is not None and row.comp_type=='1' and target.field not in row.values:
            request=receivable_request(row,base.universe_version,**options)
            requests[request['requestKey']]=request
    sources = {row.source_key: row for row in inputs.financials if row.table == 'balance'}
    zone = ZoneInfo(timezone)
    evidence = []
    with factory() as session:
        stored = session.scalars(select(Supplement).where(
            Supplement.request_key.in_(requests)).order_by(Supplement.request_key)).all()
        store = ReceivablesStore(factory,**options)
        for result in stored:
            request = requests[result.request_key]
            source = sources[request['coreBinding']['sourceKey']]
            outcome = store.recover(request, source, page_size)
            response = session.get(Response, result.response_id)
            attempt = session.get(Attempt, result.response_id)
            if response is None or attempt is None or attempt.finished_at is None or outcome is None:
                raise ValueError('ENTERPRISE_EVIDENCE_INVALID')
            def aware(value):
                return value.replace(tzinfo=UTC) if value.utcoffset() is None else value
            available = max(aware(value) for value in (response.captured_at, result.created_at, attempt.finished_at))
            identity = {'requestKey': result.request_key, 'responseHash': response.response_hash,
                        'witnessVersion': result.witness_version, 'availableAt': available.isoformat()}
            evidence.append(ReceivableInput(source.source_key, _hash(asdict(source)), outcome['status'],
                outcome['value'], _hash(identity), available.astimezone(zone).date(), result.request_key))
    return replace(base, **{destination:tuple(evidence)})
