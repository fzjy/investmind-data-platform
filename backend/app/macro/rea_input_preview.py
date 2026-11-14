"""REA五节点的纯只读补充规划；不采集、不写Core或恢复状态。"""
from collections import Counter
from dataclasses import asdict
from datetime import date, timedelta

from app.connectors.tushare.strategies import Dataset, FIELD_REGISTRY, LEGACY_CASHFLOW_FIELDS
from app.macro.enterprise import EnterpriseInputs, EnterprisePolicy, FinancialRecord, _hash, _number, _previous, _select, _top_rows
from app.macro.receivables_preview import _core_binding

CONTRACT = 'enterprise-rea-inputs-preview-v1'
NODE_IDS = ('capacity', 'renewal', 'employee', 'payable', 'debt')
SOURCE_FIELDS = {
    'balance': ('fix_assets', 'acct_payable', 'money_cap', 'st_borr', 'lt_borr', 'bond_payable', 'non_cur_liab_due_1y'),
    'cashflow': ('c_pay_acq_const_fiolta', 'depr_fa_coga_dpba', 'amort_intang_assets', 'lt_amort_deferred_exp', 'c_paid_to_for_empl'),
}
SOURCES = {'balance': (Dataset.BALANCE_SHEET, 'balancesheet'), 'cashflow': (Dataset.CASHFLOW, 'cashflow')}
META_FIELDS = frozenset(('ts_code', 'ann_date', 'f_ann_date', 'end_date', 'report_type', 'comp_type', 'update_flag'))


def field_state(values, field):
    if field not in values:
        return 'missing-field'
    if values[field] is None:
        return 'existing-null-field'
    value = _number(values[field])
    return 'existing-finite-field' if value is not None and value >= 0 else 'invalid-existing-field'


REA_REPORT_FIELDS = {
    "cashflow": LEGACY_CASHFLOW_FIELDS,
    "balance": FIELD_REGISTRY[Dataset.BALANCE_SHEET],
}


def witness_fields(table):
    return set(REA_REPORT_FIELDS[table]) - META_FIELDS


def _select_rea_source(rows, as_of):
    # 旧_select的并列判据只比较values/comp_type；补充见证还必须公告元数据一致。
    top = _top_rows(rows, as_of)
    versions = {_hash({key: value for key, value in _core_binding(row).items() if key != 'sourceKey'}) for row in top}
    if len(versions) > 1:
        return None, 'revision-conflict'
    return _select(rows, as_of)


def rea_input_request(row: FinancialRecord, pool_version: str) -> dict:
    if row.table not in SOURCES or row.report_type != '1' or row.comp_type != '1':
        raise ValueError('ENTERPRISE_REA_SOURCE_INVALID')
    missing = [field for field in SOURCE_FIELDS[row.table] if field not in row.values]
    if not missing or not witness_fields(row.table) <= set(row.values):
        raise ValueError('ENTERPRISE_REA_SOURCE_WITNESS_REQUIRED')
    _, api = SOURCES[row.table]
    request = {
        'contractVersion': CONTRACT, 'poolVersion': pool_version, 'table': row.table,
        'apiName': api, 'params': {'ts_code': row.symbol, 'period': row.end_date.strftime('%Y%m%d'), 'report_type': '1'},
        'fields': list(dict.fromkeys((*REA_REPORT_FIELDS[row.table], *SOURCE_FIELDS[row.table]))),
        'missingFields': missing, 'coreBinding': _core_binding(row),
        'coreBindingStatus': 'requires-response-witness-match',
    }
    return {**request, 'requestKey': _hash(request)}


def preview_rea_inputs(inputs: EnterpriseInputs, as_of: date, policy: EnterprisePolicy,
                       quarters: int, request_limit: int) -> dict:
    if (type(quarters) is not int or not 1 <= quarters <= 8
            or type(request_limit) is not int or not 1 <= request_limit <= 1000):
        raise ValueError('ENTERPRISE_REA_PREVIEW_LIMIT_INVALID')
    symbols = tuple(sorted(inputs.symbols))
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError('ENTERPRISE_REA_UNIVERSE_INVALID')
    if any(row.symbol not in symbols for rows in (inputs.securities, inputs.financials, inputs.industries) for row in rows):
        raise ValueError('ENTERPRISE_REA_SCOPE_INVALID')
    securities = {row.symbol: row for row in inputs.securities}
    if len(securities) != len(inputs.securities):
        raise ValueError('ENTERPRISE_REA_SECURITIES_INVALID')
    period = date(as_of.year, ((as_of.month - 1) // 3) * 3 + 1, 1) - timedelta(days=1)
    outputs = []
    for _ in range(min(quarters, policy.max_quarters)):
        outputs.append(period)
        period = _previous(period)
    outputs.sort()
    balances = {p for p in outputs} | {p.replace(year=p.year - 1) for p in outputs}
    periods = {'balance': sorted(balances),
               'cashflow': sorted(balances | {_previous(p) for p in balances if p.month != 3})}
    grouped = {}
    for row in inputs.financials:
        if row.table in ('income', *SOURCES):
            grouped.setdefault((row.table, row.symbol, row.end_date), []).append(row)
    decisions, requests = [], []
    for table in SOURCES:
        for symbol in symbols:
            for period in periods[table]:
                candidates = tuple(grouped.get((table, symbol, period), ()))
                row, _ = _select_rea_source(candidates, as_of)
                related = [_select_rea_source(tuple(grouped.get((kind, symbol, period), ())), as_of)
                           for kind in ('income', 'balance', 'cashflow')]
                company_types = {r.comp_type for r, _ in related if r is not None}
                security = securities.get(symbol)
                roots = [i.root_code for i in inputs.industries if i.symbol == symbol and i.effective_from <= period
                         and (i.effective_to is None or i.effective_to >= period)]
                states = {field: field_state(row.values, field) for field in SOURCE_FIELDS[table]} if row else {}
                if security and security.list_date and security.list_date > period:
                    reason = 'before-listing'
                elif any(e == 'revision-conflict' for _, e in related):
                    reason = 'revision-conflict'
                elif company_types & {'2', '3', '4', '7'} or roots in (['801780.SI'], ['801790.SI']):
                    reason = 'financial-report'
                elif len(company_types) > 1:
                    reason = 'company-type-conflict'
                elif not security or not security.list_date:
                    reason = 'unknown-listing-date'
                elif len(roots) != 1 or roots[0] is None:
                    reason = 'unknown-or-overlapping-industry'
                elif row is None:
                    reason = 'core-version-missing'
                elif row.comp_type != '1':
                    reason = 'incompatible-company-type'
                elif not witness_fields(table) <= set(row.values):
                    reason = 'source-witness-missing'
                else:
                    reason = 'supplement-required' if 'missing-field' in states.values() else 'no-absent-fields'
                decision = {'table': table, 'symbol': symbol, 'period': period.isoformat(), 'reason': reason,
                            'fieldStates': states, 'coreBinding': _core_binding(row) if row else None,
                            'sourceEvidenceHash': _hash(sorted(_hash(asdict(r)) for r in candidates))}
                if reason == 'supplement-required':
                    request = rea_input_request(row, inputs.universe_version)
                    requests.append(request)
                    decision['requestKey'] = request['requestKey']
                decisions.append(decision)
    identity = {'contract': CONTRACT, 'poolVersion': inputs.universe_version, 'symbols': symbols,
                'outputPeriods': [p.isoformat() for p in outputs], 'quarters': quarters, 'policy': asdict(policy),
                'requestLimit': request_limit, 'decisions': decisions, 'requests': requests,
                'securities': sorted(_hash(asdict(r)) for r in inputs.securities),
                'industries': sorted(_hash(asdict(r)) for r in inputs.industries),
                'financials': sorted(_hash(asdict(r)) for r in inputs.financials)}
    counts = {}
    for table, fields in SOURCE_FIELDS.items():
        for field in fields:
            counts[f'{table}.{field}'] = dict(sorted(Counter(d['fieldStates'][field] for d in decisions
                if d['table'] == table and field in d['fieldStates']
                and d['reason'] in ('supplement-required', 'no-absent-fields', 'source-witness-missing')).items()))
    return {'status': 'preview', 'schemaVersion': '1.0', 'contractVersion': CONTRACT,
            'asOfDate': as_of.isoformat(), 'readOnly': True, 'physicalHttpExecuted': 0,
            'poolVersion': inputs.universe_version, 'poolCount': len(symbols), 'nodeIds': list(NODE_IDS),
            'policy': asdict(policy), 'quartersRequested': quarters, 'outputPeriods': identity['outputPeriods'],
            'sourcePeriods': {table: [p.isoformat() for p in dates] for table, dates in periods.items()},
            'targetFields': {table: list(fields) for table, fields in SOURCE_FIELDS.items()},
            'planFingerprint': _hash(identity), 'decisions': decisions, 'requests': requests,
            'decisionCounts': dict(sorted(Counter(d['reason'] for d in decisions).items())),
            'fieldCounts': counts, 'fieldCountsScope': 'eligible-core-decisions',
            'logicalRequestCount': len(requests), 'requestCountsByTable': dict(sorted(Counter(r['table'] for r in requests).items())),
            'physicalHttpLimit': request_limit, 'nextBatchCandidates': requests[:request_limit],
            'physicalBudgetEnforced': False, 'persistenceImplemented': False, 'permissionsVerified': False,
            'notImplemented': ['durable-storage', 'physical-budget-executor', 'supplier-permission-probe',
                               'collection', 'durable-resume', 'supplement-consumption', 'publication']}
