"""剩余目录目标注册；共享既有官方限制，不新增预算副本。"""
from typing import Literal

from pydantic import Field,model_validator

from app.macro.official_member_policy import StrictPolicy,OfficialSource,load_official_policy
from app.macro.fund_member_graph import FundMemberGraphScope
from app.macro.issuer_contract_samples import IssuerSampleSource
from app.macro.issuer_sample_recovery import RecoveryDocument
from app.macro.nffund_contracts import CONTRACTS as NFFUND_CONTRACTS,METADATA_URL
from app.macro.fsfund_contracts import FUND_CODE as BANK_FUND_CODE,METADATA_URL as BANK_METADATA_URL


class CatalogMemberTarget(StrictPolicy):
    code:str
    documents:list[OfficialSource]=Field(max_length=3)


class CatalogMemberPolicy(StrictPolicy):
    collectionEnabled:bool
    limitsReference:Literal['officialEvidence']
    targets:list[CatalogMemberTarget]=Field(min_length=1,max_length=24)
    discoveryEnabled:bool=False
    discoveryDocuments:list[OfficialSource]=Field(default_factory=list,max_length=12)
    memberGraphEnabled:bool=False
    memberGraphs:list[FundMemberGraphScope]=Field(default_factory=list,max_length=12)
    sampleEnabled:bool=False
    sampleDocuments:list[IssuerSampleSource]=Field(default_factory=list,max_length=12)
    sampleRecoveries:list[RecoveryDocument]=Field(default_factory=list,max_length=3)

    @model_validator(mode='after')
    def unique_targets(self):
        if len({t.code for t in self.targets})!=len(self.targets):raise ValueError('CATALOG_MEMBER_POLICY_INVALID')
        if self.discoveryEnabled and not self.discoveryDocuments:raise ValueError('FUND_CONTRACT_DISCOVERY_POLICY_INVALID')
        for values in ([d.id for d in self.discoveryDocuments],[d.fundCode for d in self.discoveryDocuments]):
            if len(values)!=len(set(values)):raise ValueError('FUND_CONTRACT_DISCOVERY_POLICY_INVALID')
        if self.memberGraphEnabled and not self.memberGraphs or len({g.code for g in self.memberGraphs})!=len(self.memberGraphs):
            raise ValueError('FUND_MEMBER_GRAPH_POLICY_INVALID')
        if self.sampleEnabled and not self.sampleDocuments or len({d.fundCode for d in self.sampleDocuments})!=len(self.sampleDocuments):
            raise ValueError('ISSUER_SAMPLE_POLICY_INVALID')
        recovery_codes=[d.fundCode for d in self.sampleRecoveries]
        if len(set(recovery_codes))!=len(recovery_codes) or not set(recovery_codes)<={d.fundCode for d in self.sampleDocuments}:
            raise ValueError('ISSUER_RECOVERY_POLICY_INVALID')
        return self


def derived_policy(base_policy,target,scope):
    value=base_policy.model_dump()
    # 状态开关只由实时入口决定；冻结证明不因暂停而消失，也不授予发送权限。
    value.update(collectionEnabled=True,bootstrapTargetCodes=[target.code],documents=[d.model_dump() for d in scope.documents])
    return load_official_policy(value,(target,))


def load_catalog_member_policy(value,targets,base_policy):
    if base_policy is None:raise ValueError('CATALOG_MEMBER_BASE_REQUIRED')
    policy=CatalogMemberPolicy.model_validate(value);by_code={t.code:t for t in targets}
    old=set(base_policy.bootstrapTargetCodes)
    for scope in policy.targets:
        if scope.code not in by_code or scope.code in old:raise ValueError('CATALOG_MEMBER_SCOPE_INVALID')
        target=by_code[scope.code]
        if not scope.documents:
            if target.kind=='index':raise ValueError('CATALOG_MEMBER_INDEX_SOURCE_REQUIRED')
            continue
        derived_policy(base_policy,target,scope)
    registered={s.code for s in policy.targets}
    for doc in policy.sampleDocuments:
        matches=[t for t in targets if t.code[:6]==doc.fundCode]
        scope=next((s for s in policy.targets if s.code[:6]==doc.fundCode),None)
        overlap=any(d.fundCode==doc.fundCode for d in policy.discoveryDocuments) or any(g.code[:6]==doc.fundCode for g in policy.memberGraphs)
        closed_current=doc.fundCode in NFFUND_CONTRACTS and any(d.fundCode==doc.fundCode and d.url==METADATA_URL for d in policy.discoveryDocuments)
        closed_current=closed_current or doc.fundCode==BANK_FUND_CODE and any(d.fundCode==doc.fundCode and d.url==BANK_METADATA_URL for d in policy.discoveryDocuments)
        closed_current=closed_current or doc.fundCode=='159928' and any(d.fundCode==doc.fundCode for d in policy.discoveryDocuments)
        closed_current=closed_current or doc.fundCode=='588200' and any(d.fundCode==doc.fundCode for d in policy.discoveryDocuments)
        closed_current=closed_current or doc.fundCode in ('512010','515030') and any(d.fundCode==doc.fundCode for d in policy.discoveryDocuments)
        closed_current=closed_current or doc.fundCode=='512980' and any(d.fundCode==doc.fundCode for d in policy.discoveryDocuments)
        if (len(matches)!=1 or matches[0].kind!='etf' or matches[0].code not in registered or scope is None or scope.documents
            or overlap and not closed_current):
            raise ValueError('ISSUER_SAMPLE_POLICY_INVALID')
    for doc in policy.discoveryDocuments:
        matches=[t for t in targets if t.code[:6]==doc.fundCode]
        if (doc.kind!='fund-mapping' or len(matches)!=1 or matches[0].kind!='etf'
            or matches[0].code not in registered):raise ValueError('FUND_CONTRACT_DISCOVERY_POLICY_INVALID')
    for graph in policy.memberGraphs:
        target=by_code.get(graph.code);doc=next((d for d in policy.discoveryDocuments if d.id==graph.discoveryDocumentId),None)
        scope=next((s for s in policy.targets if s.code==graph.code),None)
        if (target is None or target.kind!='etf' or graph.code in old or doc is None
            or doc.fundCode!=graph.code[:6] or scope is None or scope.documents):raise ValueError('FUND_MEMBER_GRAPH_POLICY_INVALID')
    return policy
