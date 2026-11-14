"""只读追加已验证股票原件；不改Core、补充尝试或发布状态。"""
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import re

from sqlalchemy import select, text

from app.db.models import IngestionBatch, JobCursor, PublishedSnapshot
from app.macro.enterprise import EnterpriseInputs, FinancialRecord, _hash, _number
from app.stocks.api import financial_history as supply

# 只允许现有宏观合同的人民币元金额，不携带EPS、股本或股票派生指标。
FIELDS = {
    'income': ('revenue', 'oper_cost', 'n_income', 'n_income_attr_p'),
    'balance': ('inventories', 'accounts_receiv', 'contract_liab', 'fix_assets',
                'acct_payable', 'money_cap', 'st_borr', 'lt_borr', 'bond_payable', 'non_cur_liab_due_1y'),
    'cashflow': ('n_cashflow_act', 'c_pay_acq_const_fiolta', 'c_pay_dist_dpcp_int_exp',
                 'c_paid_to_for_empl', 'depr_fa_coga_dpba', 'amort_intang_assets', 'lt_amort_deferred_exp'),
}
TABLES = {v[1]: k for k, v in supply.KINDS.items()}
CONTRACT_VERSION = 'stock-raw-macro-history-v1'


@dataclass(frozen=True)
class StockHistoryRecord(FinancialRecord):
    available_date: date
    available_at: str
    evidence_hash: str
    source_as_of: str
    source_base_version: str
    amount_unit: str = 'yuan'


def _stamp(value):
    if not isinstance(value, datetime):
        raise ValueError('STOCK_HISTORY_TIMESTAMP_INVALID')
    # 数据库SQLite测试返回无时区；生产PostgreSQL时间戳为UTC aware。
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _day(value):
    if not isinstance(value, str):
        raise ValueError('STOCK_HISTORY_METADATA_INVALID')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value and parsed.strftime('%Y%m%d') != value:
        raise ValueError('STOCK_HISTORY_METADATA_INVALID')
    return parsed


def load_stock_history_bridge(factory, inputs: EnterpriseInputs, as_of, *, enabled=False) -> EnterpriseInputs:
    return audit_stock_history_bridge(factory, inputs, as_of, enabled=enabled)[0]


def audit_stock_history_bridge(factory, inputs: EnterpriseInputs, as_of, *, enabled=False):
    audit = dict(status='disabled', requestCount=0, addedRecords=0, contractVersion=CONTRACT_VERSION)
    if enabled is False:
        return inputs, audit
    if enabled is not True:
        raise ValueError('STOCK_HISTORY_SWITCH_INVALID')
    cutoff = _stamp(as_of) if isinstance(as_of, datetime) else datetime.combine(as_of, time.min, timezone.utc)
    with factory() as session:
        if session.get_bind().dialect.name == 'postgresql':
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        base = session.get(PublishedSnapshot, 'screener')
        if base is None:
            return inputs, audit | dict(status='no-stock-publication')
        source_day = base.as_of.date()
        symbols = sorted(r['symbol'] for r in base.payload['stocks'])
        if (len(set(symbols)) != len(symbols) or len(set(inputs.symbols)) != len(inputs.symbols)
                or set(symbols) != set(inputs.symbols)):
            return inputs, audit | dict(status='scope-mismatch')
        if source_day > cutoff.date():
            return inputs, audit | dict(status='source-not-visible')
        contracts = list(session.scalars(select(JobCursor).where(JobCursor.job_name == supply.JOB,
            JobCursor.cursor_key.like('contract:%')).order_by(JobCursor.id.desc())))
        if not contracts:
            return inputs, audit | dict(status='no-contract')

        @contextmanager
        def pinned():
            yield session

        # 复用完整页链、原始HTTP字节／摘要／请求身份校验；固定在同一事务。
        chosen = None;not_visible=False
        for cursor in contracts:
            contract = cursor.cursor_value
            if cursor.cursor_key != 'contract:' + supply.content_hash(contract):
                raise ValueError('STOCK_HISTORY_CONTRACT_INVALID')
            if contract.get('symbols') != symbols or contract.get('asOf', '9999') > source_day.isoformat():
                continue
            specs = supply.expanded_requests(session, contract)
            saved = [(spec, supply.saved_response(session, contract, spec)) for spec in specs]
            # 先核验所有新尝试，失败/坏原件不可以用旧合同遮掩。
            for spec in specs:
                origin=supply.source_contract(session,contract,spec)
                unit=supply.read_cursor(session,f"{supply.content_hash(origin)}:{spec['target']}")
                if unit is not None and unit.cursor_value.get('status')!='completed':
                    raise ValueError('FINANCIAL_UNRESOLVED_PRIOR_UNIT')
            if all(item is not None for _, item in saved):
                observed=[stamp for _,(raw,_) in saved for stamp in
                    (_stamp(raw.captured_at),_stamp(session.get(IngestionBatch,raw.batch_id).finished_at))]
                if observed and (max(observed)>cutoff or max(observed).date()+timedelta(days=1)>cutoff.date()):
                    not_visible=True
                    continue
                chosen = contract, saved
                break
        if chosen is None:
            return inputs, audit | dict(status='evidence-not-visible' if not_visible else 'no-compatible-contract')
        contract, saved = chosen
        points=supply.load_points(pinned,source_day,symbols,contract_identity=supply.content_hash(contract))
        stamps = []
        witnesses = {}
        depreciation_rows=[]
        for spec, (raw, rows) in saved:
            kind = spec['target'].split(':')[0]
            origin=supply.source_contract(session,contract,spec)
            policy = contract['policy']['tables'][kind]
            if (policy['api'] != supply.KINDS[kind][0] or not supply.META <= set(policy['fields'])
                    or policy.get('amountUnit', 'yuan') != 'yuan'):
                raise ValueError('STOCK_HISTORY_UNIT_CONTRACT_INVALID')
            batch = session.get(IngestionBatch, raw.batch_id)
            stamps.extend((_stamp(raw.captured_at), _stamp(batch.finished_at)))
            for index, row in enumerate(rows, 1):
                witnesses[f'raw.source_payload:{raw.id}:row:{index}'] = (raw.content_hash,row,origin)
                if kind=='cashflow':
                    try:
                        end=_day(str(row['end_date']))
                        dates=[_day(str(row[k])) for k in ('ann_date','f_ann_date') if row.get(k)]
                        if ((end.month,end.day) in ((3,31),(6,30),(9,30),(12,31)) and dates
                            and end<=_day(contract['asOf']) and all(end<=d<=_day(contract['asOf']) for d in dates)):
                            depreciation_rows.append((f'raw.source_payload:{raw.id}:row:{index}',raw.content_hash,row))
                    except (ValueError,KeyError,TypeError):pass
        if not stamps:
            return inputs, audit | dict(status='no-evidence')
        available = max(stamps)
        # 日期型投影保守从下一UTC日开始可见；绝不以捕获时间重排公告版本。
        visible_day = available.date() + timedelta(days=1)
        audit.update(sourceAsOf=contract['asOf'], sourceBaseVersion=contract['baseVersion'],
            availableAt=available.isoformat(), availableDate=visible_day.isoformat(), pageCount=len(saved))
        if available > cutoff or visible_day > cutoff.date():
            return inputs, audit | dict(status='evidence-not-visible')
        occupied = {(r.table, r.symbol, r.end_date) for r in inputs.financials}
        appended = []
        skipped = Counter()
        blocked = set()
        for point in points:
            candidate = None
            try:
                kind = TABLES[point.source_table]
                values = point.values
                key = values['_sourceId']
                if not re.fullmatch(r'raw\.source_payload:\d+:row:\d+', key) or key not in witnesses:
                    raise ValueError('source-identity')
                content_hash, original, origin = witnesses[key]
                end = _day(str(original['end_date']))
                if (original.get('report_type') == '1' and point.ts_code in symbols
                        and original.get('ts_code') == point.ts_code and end == point.end_date
                        and (end.month, end.day) in ((3,31), (6,30), (9,30), (12,31))):
                    candidate = (kind, point.ts_code, end)
                    if candidate in occupied:
                        skipped['existing-tuple'] += 1
                        continue
                if (values['_sourceAsOf'] != origin['asOf'] or values['_sourceBaseVersion'] != origin['baseVersion']
                        or point.ts_code not in symbols or original['ts_code'] != point.ts_code
                        or point.report_type != '1' or original['report_type'] != '1'
                        or original['comp_type'] not in ('1', '2', '3', '4', '7')
                        or str(original['update_flag']) not in ('0', '1')):
                    raise ValueError('metadata')
                ann = _day(str(original['ann_date'])) if original.get('ann_date') else None
                fann = _day(str(original['f_ann_date'])) if original.get('f_ann_date') else None
                if (end != point.end_date or (end.month, end.day) not in ((3,31), (6,30), (9,30), (12,31))
                        or not (ann or fann) or any(d < end or d > _day(contract['asOf']) for d in (ann, fann) if d)
                        or end > _day(contract['asOf'])):
                    raise ValueError('dates')
                if (kind, point.ts_code, end) in occupied:
                    skipped['existing-tuple'] += 1
                    continue
                amounts = {k: original[k] for k in FIELDS[kind] if k in original}
                if any(v is not None and _number(v) is None for v in amounts.values()):
                    raise ValueError('nonfinite-amount')
                # 保留整份报告的同版冲突判据；逐行sourceId不参与相同事实判断。
                amounts['_stockHistoryWitnessHash'] = _hash({k:v for k,v in original.items()
                    if k not in supply.META and k != 'sourceId'})
                appended.append(StockHistoryRecord(kind, point.ts_code, end, ann, fann, '1',
                    original['comp_type'], str(original['update_flag']), amounts, key, visible_day,
                    available.isoformat(), _hash(dict(rawHash=content_hash, row=original,
                        contract=supply.content_hash(contract), availableAt=available.isoformat())),
                    origin['asOf'], origin['baseVersion']))
            except (KeyError, ValueError, TypeError):
                skipped['invalid-row'] += 1
                if candidate is not None:
                    blocked.add(candidate)
        # 无效标准季度行不能消失后让同组旧版/同版变成唯一可信报告。
        # 保守隔离整组；不回写Core，不把非季末或其他报告类型误当修订。
        appended = [r for r in appended if (r.table, r.symbol, r.end_date) not in blocked]
        audit.update(status='appended' if appended else 'no-new-records', addedRecords=len(appended),
            skipped=dict(skipped), blockedTupleCount=len(blocked),
            blockedTuples=[dict(table=t, symbol=s, period=d.isoformat()) for t,s,d in sorted(blocked)],
            evidenceHash=_hash([r.evidence_hash for r in appended]))
        result=replace(inputs,financials=(*inputs.financials,*appended)) if appended else inputs
        from app.macro.stock_depreciation_bridge import bridge_depreciation
        result,coverage=bridge_depreciation(result,depreciation_rows,available_date=visible_day,
            contract_hash=supply.content_hash(contract))
        audit['depreciationSupplement']=coverage
        return result,audit
