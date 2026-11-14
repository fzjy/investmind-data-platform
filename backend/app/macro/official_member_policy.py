"""官方成员独立来源策略；不改变既有Tushare目录或预算身份。"""
from datetime import date
from hashlib import sha256
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.macro.nffund_contracts import CONTRACTS as NFFUND_CONTRACTS,METADATA_URL,pcf_template
from app.macro import fsfund_contracts as fsfund
from app.macro import consumer_xml_contracts as consumer
from app.macro import jsfund_contracts as jsfund
from app.macro import etf_definition_contracts as definition
from app.macro import media_contracts as media


class StrictPolicy(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,frozen=True)


class OfficialSource(StrictPolicy):
    id: str
    kind: Literal['csi-members','fund-mapping','fund-pcf']
    indexCode: str
    fundCode: str | None
    subscriptionCode: str | None
    tradingDate: str | None
    url: str

    @model_validator(mode='after')
    def closed_source(self):
        if not re.fullmatch(r'[a-z0-9-]{1,40}',self.id) or not re.fullmatch('[0-9]{6}',self.indexCode):
            raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
        if self.kind=='csi-members':
            expected=f'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/{self.indexCode}cons.xls'
            if any(v is not None for v in (self.fundCode,self.subscriptionCode,self.tradingDate)) or self.url!=expected:
                raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
        else:
            if self.fundCode is None or re.fullmatch('[0-9]{6}',self.fundCode) is None:
                raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
            if self.fundCode==media.FUND_CODE:
                if self.indexCode!=media.INDEX_CODE:raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                if self.kind=='fund-mapping':
                    if self.subscriptionCode is not None or self.tradingDate!=media.STATEMENT_DATE or self.url!=media.PDF_URL:raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                elif (self.subscriptionCode!=media.FUND_CODE or self.tradingDate is None
                    or date.fromisoformat(self.tradingDate).isoformat()!=self.tradingDate or self.url!=media.PCF_URL):raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                return self
            if self.fundCode in definition.PROFILES:
                if (self.indexCode!=definition.PROFILES[self.fundCode][0] or self.tradingDate is None
                    or self.url!=definition.source_url(self.fundCode,self.tradingDate)
                    or self.subscriptionCode!=(None if self.kind=='fund-mapping' else self.fundCode)):
                    raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                return self
            if self.fundCode==jsfund.FUND_CODE:
                if (self.indexCode!=jsfund.INDEX_CODE or self.url!=jsfund.URL
                    or self.subscriptionCode!=(None if self.kind=='fund-mapping' else jsfund.FUND_CODE)
                    or (self.tradingDate is not None if self.kind=='fund-mapping' else self.tradingDate is None
                        or date.fromisoformat(self.tradingDate).isoformat()!=self.tradingDate)):
                    raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                return self
            if self.fundCode==consumer.FUND_CODE:
                if (self.indexCode!=consumer.INDEX_CODE or self.tradingDate is None
                    or self.url!=consumer.pcf_url(self.tradingDate)
                    or self.subscriptionCode!=(None if self.kind=='fund-mapping' else consumer.FUND_CODE)):
                    raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                return self
            if self.fundCode==fsfund.FUND_CODE:
                if self.indexCode!=fsfund.INDEX_CODE:raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                if self.kind=='fund-mapping':
                    if self.subscriptionCode is not None or self.tradingDate is not None or self.url!=fsfund.METADATA_URL:
                        raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                elif (self.subscriptionCode!=fsfund.FUND_CODE or self.tradingDate is None
                    or date.fromisoformat(self.tradingDate).isoformat()!=self.tradingDate or self.url!=fsfund.PCF_URL):
                    raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                return self
            if self.fundCode in NFFUND_CONTRACTS:
                if self.indexCode!=NFFUND_CONTRACTS[self.fundCode][0]:raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                if self.kind=='fund-mapping':
                    if self.subscriptionCode is not None or self.tradingDate is not None or self.url!=METADATA_URL:
                        raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                elif (self.tradingDate is None or date.fromisoformat(self.tradingDate).isoformat()!=self.tradingDate
                    or self.url!=pcf_template(self.fundCode,self.subscriptionCode).format(yyyymmdd=self.tradingDate.replace('-',''))):
                    raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
                return self
            if self.kind=='fund-mapping':
                if self.subscriptionCode is not None or self.tradingDate is not None or self.url!=f'https://e.gtfund.com/etrade/Jijin/view/id/{self.fundCode}#etf-list':
                    raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
            else:
                if (self.subscriptionCode is None or re.fullmatch('[0-9]{6}',self.subscriptionCode) is None
                    or self.tradingDate is None or date.fromisoformat(self.tradingDate).isoformat()!=self.tradingDate
                    or self.url!=f'https://m.gtfund.com/cochin/etf/download/{self.subscriptionCode}/{self.tradingDate.replace("-","")}'):
                    raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
        return self


class OfficialMemberPolicy(StrictPolicy):
    collectionEnabled: bool
    # 运行时日限开关不属于冻结来源身份，改变它不得使旧原件或游标失效。
    dailyBudgetEnabled: bool = Field(default=True,exclude=True)
    useScope: Literal['local-development-only']
    bootstrapTargetCodes: list[str]=Field(min_length=1,max_length=2)
    maxHttpPerRun: int=Field(ge=1,le=4)
    maxHttpPerDay: int=Field(ge=1,le=4)
    requestsPerMinute: int=Field(ge=1,le=10)
    requestTimeoutSeconds: int=Field(ge=1,le=20)
    maxRetries: int=Field(ge=0,le=0)
    maxResponseBytes: int=Field(ge=1,le=8388608)
    maxSnapshotMembers: int=Field(ge=1,le=10000)
    documents: list[OfficialSource]=Field(min_length=1,max_length=4)

    @model_validator(mode='after')
    def unique_sources(self):
        for items in (self.bootstrapTargetCodes,[d.id for d in self.documents],[d.url for d in self.documents]):
            if len(items)!=len(set(items)): raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
        return self

    @property
    def fingerprint(self):
        return sha256(json.dumps(self.model_dump(),ensure_ascii=False,sort_keys=True,
            separators=(',',':'),allow_nan=False).encode()).hexdigest()


def load_official_policy(value,targets):
    policy=OfficialMemberPolicy.model_validate(value)
    by_code={t.code:t for t in targets}
    required=set()
    for code in policy.bootstrapTargetCodes:
        if code not in by_code: raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
        target=by_code[code]
        if target.kind=='index':
            matches=[d for d in policy.documents if d.kind=='csi-members' and d.indexCode==code[:6]]
        else:
            matches=[d for d in policy.documents if d.fundCode==code[:6]]
            if {d.kind for d in matches}!={'fund-mapping','fund-pcf'} or len({d.indexCode for d in matches})!=1:
                raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
            matches += [d for d in policy.documents if d.kind=='csi-members' and d.indexCode==matches[0].indexCode]
        if not matches or len([d for d in matches if d.kind=='csi-members'])!=1:
            raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
        required.update(d.id for d in matches)
    if required!={d.id for d in policy.documents}: raise ValueError('OFFICIAL_MEMBER_POLICY_INVALID')
    return policy
