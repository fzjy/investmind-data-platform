"""单根冻结及按已验证父响应推导的有限请求图；无I/O。"""
from calendar import monthrange
from collections.abc import Mapping
from datetime import date
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.macro.target_evidence_contract import (
    API_FIELDS, TargetEvidenceRequest, evidence_hash, target_request_hash, target_request_payload,
)
from app.macro.target_evidence_normalize import NORMALIZER_VERSION, validated_policy
from app.macro.target_evidence_policy import load_target_evidence_policy, target_evidence_policy_hash, target_evidence_policy_payload

CONTRACT = 'enterprise-target-execution-v1'
SNAPSHOT_KEYS = {'contractVersion','catalogHash','policyHash','policy','pool','timezone',
    'frozenAsOfDate','normalizerVersion','targets','windows'}


def _invalid():
    raise ValueError('TARGET_EXECUTION_INVALID')


def _windows(as_of, months):
    windows=[]
    for offset in range(months):
        total=as_of.year*12+as_of.month-1-offset
        year,month=divmod(total,12); month+=1
        start=date(year,month,1); end=min(as_of,date(year,month,monthrange(year,month)[1]))
        windows.append([start.strftime('%Y%m%d'),end.strftime('%Y%m%d')])
    return windows


def freeze_target_plan(catalog, selection, as_of: date, timezone: str) -> dict:
    try:
        policy=validated_policy(catalog.evidence_policy)
        if (type(as_of) is not date or not isinstance(timezone,str) or not selection.symbols
            or len(set(selection.symbols))!=len(selection.symbols)
            or any(not isinstance(s,str) or re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)',s) is None for s in selection.symbols)):
            _invalid()
        ZoneInfo(timezone)
        by_code={target.code:target for target in catalog.targets}
        targets=[{'code':code,'kind':by_code[code].kind} for code in policy.bootstrap_target_codes]
        snapshot={'contractVersion':CONTRACT,'catalogHash':catalog.fingerprint,
            'policyHash':target_evidence_policy_hash(policy),'policy':target_evidence_policy_payload(policy),
            'pool':{**selection.to_payload(),'symbols':sorted(selection.symbols)},
            'timezone':timezone,'frozenAsOfDate':as_of.isoformat(),'normalizerVersion':NORMALIZER_VERSION,
            'targets':targets,'windows':_windows(as_of,policy.lookback_months)}
        return {'fingerprint':evidence_hash(snapshot),'snapshot':snapshot}
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,ZoneInfoNotFoundError):
        _invalid()


def validate_frozen_root(root):
    try:
        if (not isinstance(root,dict) or set(root)!={'fingerprint','snapshot'}
            or not isinstance(root['snapshot'],dict) or set(root['snapshot'])!=SNAPSHOT_KEYS
            or root['fingerprint']!=evidence_hash(root['snapshot'])):
            _invalid()
        value=root['snapshot']; targets=value['targets']
        if value['contractVersion']!=CONTRACT or value['normalizerVersion']!=NORMALIZER_VERSION:
            _invalid()
        policy=load_target_evidence_policy(value['policy'],tuple(t['code'] for t in targets))
        as_of=date.fromisoformat(value['frozenAsOfDate']); ZoneInfo(value['timezone'])
        if (value['policyHash']!=target_evidence_policy_hash(policy)
            or value['windows']!=_windows(as_of,policy.lookback_months)
            or [t['code'] for t in targets]!=list(policy.bootstrap_target_codes)
            or len({t['code'] for t in targets})!=len(targets)
            or any(set(t)!={'code','kind'} or t['kind'] not in ('index','etf') for t in targets)):
            _invalid()
        return root
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,ZoneInfoNotFoundError):
        _invalid()


def validate_target_plan(root,catalog,selection,timezone,today) -> dict:
    validate_frozen_root(root)
    try:
        frozen=date.fromisoformat(root['snapshot']['frozenAsOfDate'])
        if type(today) is not date or today<frozen:
            _invalid()
        expected=freeze_target_plan(catalog,selection,frozen,timezone)
        if expected!=root:
            raise ValueError('TARGET_FROZEN_PLAN_CHANGED')
        return root
    except (TypeError,KeyError,AttributeError):
        _invalid()


def execution_step(root, request: TargetEvidenceRequest, parents) -> dict:
    if (not isinstance(parents,list) or len(set(parents))!=len(parents)
        or any(not isinstance(p,str) or re.fullmatch('[0-9a-f]{64}',p) is None for p in parents)):
        _invalid()
    value={'request':target_request_payload(request),'parentObservationIds':list(parents)}
    return {'requestKey':evidence_hash({'planFingerprint':root['fingerprint'],**value}),**value}


def step_request(step) -> TargetEvidenceRequest:
    try:
        value=step['request']
        if set(value)!={'targetCode','targetKind','apiName','params','fields'}:
            _invalid()
        return TargetEvidenceRequest(value['targetCode'],value['targetKind'],value['apiName'],value['params'],tuple(value['fields']))
    except (ValueError,TypeError,KeyError,AttributeError):
        _invalid()


def derive_target_steps(root,outcomes) -> dict:
    validate_frozen_root(root)
    if not isinstance(outcomes,Mapping): _invalid()
    used=set(); next_step=None; gaps=[]; observed=0

    def outcome(request,parents):
        nonlocal next_step
        step=execution_step(root,request,parents); key=step['requestKey']
        if key not in outcomes:
            if next_step is None: next_step=step
            return None
        used.add(key)
        try:
            value=outcomes[key]; body=value['payload']
            if (set(value)!={'observationId','payload'} or not isinstance(value['observationId'],str)
                or re.fullmatch('[0-9a-f]{64}',value['observationId']) is None
                or body['requestHash']!=target_request_hash(request)
                or body['contractVersion']!=NORMALIZER_VERSION or body['asOf']!=root['snapshot']['frozenAsOfDate']
                or body['timezone']!=root['snapshot']['timezone'] or body['membershipReady'] is not False
                or body['completeness']!='not_proven' or body['sourceConstituentCount'] is not None
                or body['qualityStatus'] not in ('valid_candidate','unavailable','unsupported','rejected')):
                _invalid()
            if request.api_name!='index_weight' and body['qualityStatus']=='valid_candidate':
                if body['metadata']['ts_code']!=request.params['ts_code']: _invalid()
            return value
        except (TypeError,KeyError,AttributeError):
            _invalid()

    def request(target,api,params):
        return TargetEvidenceRequest(target['code'],target['kind'],api,params,API_FIELDS[api])

    def gap(target,value):
        reasons=value['payload'].get('reasons')
        if not isinstance(reasons,list) or not reasons or not isinstance(reasons[0],str): _invalid()
        gaps.append({'targetCode':target['code'],'reason':reasons[0]})

    for target in root['snapshot']['targets']:
        parents=[]; source=target['code']
        if target['kind']=='etf':
            mapping=outcome(request(target,'etf_basic',{'ts_code':target['code']}),parents)
            if mapping is None: continue
            if mapping['payload']['qualityStatus']!='valid_candidate': gap(target,mapping); continue
            source=mapping['payload']['metadata']['index_code']; parents=[mapping['observationId']]
        metadata=outcome(request(target,'index_basic',{'ts_code':source,'market':root['snapshot']['policy']['indexMetadataMarket']}),parents)
        if metadata is None: continue
        if metadata['payload']['qualityStatus']!='valid_candidate': gap(target,metadata); continue
        parents=[*parents,metadata['observationId']]
        for start,end in root['snapshot']['windows']:
            weights=outcome(request(target,'index_weight',{'index_code':source,'start_date':start,'end_date':end}),parents)
            if weights is None: break
            body=weights['payload']
            if body['qualityStatus']=='valid_candidate':
                if not body.get('snapshotCandidates'): _invalid()
                observed+=1; break
            if body['qualityStatus']!='unavailable': gap(target,weights); break
        else:
            gaps.append({'targetCode':target['code'],'reason':'NO_MEMBERS'})
    if used!=set(outcomes): _invalid()
    return {'next':next_step,'processingCompleted':next_step is None,'terminalGaps':gaps,'observedTargetCount':observed}
