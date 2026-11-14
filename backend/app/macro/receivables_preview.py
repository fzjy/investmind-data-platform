"""白条必要字段的纯规划；没有客户端、数据库写入或完成游标。"""
from collections import Counter
from dataclasses import asdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json

from app.connectors.tushare.strategies import Dataset, FIELD_REGISTRY
from app.macro.enterprise import EnterpriseInputs, EnterprisePolicy, FinancialRecord, _select, build_enterprise_analysis
from app.macro.balance_supplement import RECEIVABLES, target_options


CONTRACT = 'enterprise-receivables-preview-v1'
TARGET_FIELD = 'accounts_receiv'


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,default=str,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def _finite(value):
    if value is None or isinstance(value,bool): return False
    try: return Decimal(str(value)).is_finite()
    except (InvalidOperation,ValueError): return False


def _core_binding(row: FinancialRecord) -> dict:
    return {'sourceKey':row.source_key,'symbol':row.symbol,'period':row.end_date.isoformat(),
        'annDate':row.ann_date.isoformat() if row.ann_date else None,
        'fAnnDate':row.f_ann_date.isoformat() if row.f_ann_date else None,
        'reportType':row.report_type,'compType':row.comp_type,'updateFlag':row.update_flag,
        'valueHash':_digest(row.values)}


def receivable_request(row: FinancialRecord, pool_version: str, *, target=RECEIVABLES) -> dict:
    """采集和只读消费共用不可变目标身份，不由其他卡片的成功状态决定。"""
    target_options(target)
    fields=list(dict.fromkeys((*FIELD_REGISTRY[Dataset.BALANCE_SHEET],target.field)))
    request={'apiName':'balancesheet','params':{'ts_code':row.symbol,'period':row.end_date.strftime('%Y%m%d'),'report_type':'1'},
        'fields':fields,'coreBinding':_core_binding(row),'coreBindingStatus':'requires-response-witness-match'}
    request['requestKey']=_digest({'contract':target.contract,'poolVersion':pool_version,**request})
    return request


def preview_receivables(inputs: EnterpriseInputs, as_of: date, policy: EnterprisePolicy,
                        quarters: int, request_limit: int, *, target=RECEIVABLES) -> dict:
    options=target_options(target)
    if type(quarters) is not int or not 1<=quarters<=8 or type(request_limit) is not int or not 1<=request_limit<=1000:
        raise ValueError('invalid receivables preview limits')
    analysis=build_enterprise_analysis(inputs,as_of,policy)
    outputs=sorted({p['period'] for c in analysis['cards'] for p in c['points']})[-quarters:]
    periods=sorted({date.fromisoformat(p) for p in outputs}|{
        date(date.fromisoformat(p).year-1,date.fromisoformat(p).month,date.fromisoformat(p).day) for p in outputs})
    securities={s.symbol:s for s in inputs.securities}
    grouped={}
    for row in inputs.financials:
        if row.table=='balance': grouped.setdefault((row.symbol,row.end_date),[]).append(row)
    requests=[]; decisions=[]
    for symbol in sorted(inputs.symbols):
        for period in periods:
            candidates=tuple(grouped.get((symbol,period),()))
            row,error=_select(candidates,as_of)
            security=securities.get(symbol)
            active=[i for i in inputs.industries if i.symbol==symbol and i.effective_from<=period and (i.effective_to is None or i.effective_to>=period)]
            root=active[0].root_code if len(active)==1 else None
            if error=='revision-conflict': reason=error
            elif security is not None and security.list_date is not None and security.list_date>period: reason='before-listing'
            elif row is not None and row.comp_type in ('2','3','4','7'): reason='financial-report'
            elif root in ('801780.SI','801790.SI'): reason='financial-industry-review'
            elif security is None or security.list_date is None: reason='unknown-listing-date'
            elif root is None: reason='unknown-or-overlapping-industry'
            elif row is None: reason='core-version-missing'
            elif row.comp_type!='1': reason='incompatible-company-type'
            elif target.field in row.values:
                value=row.values[target.field]
                reason='existing-null-field' if value is None else 'existing-finite-field' if _finite(value) else 'invalid-existing-field'
            else: reason='supplement-required'
            binding=_core_binding(row) if row is not None else None
            # 冲突/不可见时不选一行冒充依据；仍摘要全部候选，识别源修订。
            evidence_hash=_digest(sorted(_digest(_core_binding(candidate)) for candidate in candidates))
            decision={'symbol':symbol,'period':period.isoformat(),'reason':reason,
                'coreBinding':binding,'sourceEvidenceHash':evidence_hash}
            if reason=='supplement-required':
                request=receivable_request(row,inputs.universe_version,**options)
                requests.append(request);decision['requestKey']=request['requestKey']
            decisions.append(decision)
    identity={'contract':target.contract,'poolVersion':inputs.universe_version,'symbols':sorted(inputs.symbols),
        'outputPeriods':outputs,'policy':asdict(policy),'quarters':quarters,'requestLimit':request_limit,
        'requests':requests,'decisions':decisions}
    return {'status':'preview','schemaVersion':'1.0','contractVersion':target.contract,
        'asOfDate':as_of.isoformat(),'readOnly':True,'physicalHttpExecuted':0,
        'poolVersion':inputs.universe_version,'poolCount':len(inputs.symbols),'targetField':target.field,
        'quartersRequested':quarters,'outputPeriods':outputs,'balancePeriods':[p.isoformat() for p in periods],
        'planFingerprint':_digest(identity),'policy':asdict(policy),'requests':requests,'decisions':decisions,
        'decisionCounts':dict(sorted(Counter(d['reason'] for d in decisions).items())),
        'logicalRequestCount':len(requests),'physicalHttpLimit':request_limit,
        'nextBatchCandidates':requests[:request_limit],'requiresPaginationOrTruncationCheck':True,
        'physicalBudgetEnforced':False,'persistenceImplemented':False,'permissionsVerified':False,
        'notImplemented':['supplement-storage','physical-budget-executor','durable-resume','supplier-permission-probe','collection','white-strip-calculation','publication']}
