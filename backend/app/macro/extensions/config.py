"""闭合扩展策略。错误不包含配置原值或路径。"""
import json
from datetime import date
from pathlib import Path
import re
from typing import Literal

from pydantic import Field, model_validator

from app.macro.extensions.policy_types import StrictModel,Schedule
from app.macro.exchange_summary_config import ExchangeSummaryPolicy,EXCHANGE_SUMMARY_GROUPS,validate_exchange_policy
from app.macro.sz_market_sample import SzMarketSamplePolicy


DEFAULT_EXTENSION_CONFIG_FILE = Path(__file__).resolve().parents[3] / 'config' / 'macro-extensions.json'


class ExtensionError(ValueError):
    pass


class Limits(StrictModel):
    maxHttpPerRun: int = Field(ge=1, le=1)
    maxHttpPerPeriod: int = Field(ge=1, le=1)
    requestsPerMinute: int = Field(ge=1, le=10)
    requestTimeoutSeconds: int = Field(ge=1, le=20)
    maxRetries: int = Field(ge=0, le=0)
    maxResponseBytes: int = Field(ge=1, le=8388608)
    maxResponseRows: int = Field(ge=1, le=366)


class GroupPolicy(StrictModel):
    collectionEnabled: bool
    schedule: Schedule
    lookbackPeriods: int = Field(ge=1, le=366)
    quoteSide: Literal['bid_close', 'ask_close'] | None
    limits: Limits


class FiscalLimits(StrictModel):
    maxHttpPerRun: int = Field(ge=1, le=5)
    maxHttpPerPeriod: int = Field(ge=1, le=5)
    requestsPerMinute: int = Field(ge=1, le=10)
    requestTimeoutSeconds: int = Field(ge=1, le=20)
    maxRetries: int = Field(ge=0, le=0)
    maxResponseBytes: int = Field(ge=1, le=5242880)
    maxResponseRows: int = Field(ge=1, le=12)


class FiscalDocument(StrictModel):
    year: int = Field(ge=1000, le=9999)
    url: str

    @model_validator(mode='after')
    def official_url(self):
        if not re.fullmatch(r'https://www\.pbc\.gov\.cn/diaochatongjisi/attachDir/[0-9]{4}/(?:0[1-9]|1[0-2])/[0-9]{17,22}\.htm', self.url):
            raise ValueError('invalid official document')
        return self


class FiscalPolicy(StrictModel):
    collectionEnabled: bool
    schedule: Schedule
    lookbackPeriods: int = Field(ge=1, le=48)
    quoteSide: None
    limits: FiscalLimits
    documents: list[FiscalDocument] = Field(min_length=1, max_length=5)

    @model_validator(mode='after')
    def unique_documents(self):
        if (len({d.year for d in self.documents}) != len(self.documents)
            or len({d.url for d in self.documents}) != len(self.documents)):
            raise ValueError('duplicate official document')
        return self


class OfficialMacroLimits(StrictModel):
    maxHttpPerRun: int = Field(ge=1, le=8)
    maxHttpPerPeriod: int = Field(ge=1, le=8)
    requestsPerMinute: int = Field(ge=1, le=10)
    requestTimeoutSeconds: int = Field(ge=1, le=20)
    maxRetries: int = Field(ge=0, le=0)
    maxResponseBytes: int = Field(ge=1, le=5242880)
    maxResponseRows: int = Field(ge=1, le=64)
    maxDirectoryCandidates: int = Field(ge=1, le=2)


class OfficialMacroDocument(StrictModel):
    id: str
    role: Literal['household-ytd','household-directory','tier1-prices','tier1-directory','tier1-directory-page','special-quota','special-budget-directory','special-year-directory','special-directory','special-issuance','special-briefing','property-directory','property-report']
    period: str
    url: str | None
    derivedFrom: str | None

    @model_validator(mode='after')
    def closed_document(self):
        if re.fullmatch(r'[a-z][a-z0-9-]{0,47}',self.id) is None:raise ValueError('invalid document id')
        if self.role=='special-budget-directory':
            if self.period!='latest' or self.url!='https://www.mof.gov.cn/zyyjsgkpt/zyzfyjs/zyys/ysbb/' or self.derivedFrom is not None:raise ValueError('invalid annual root')
            return self
        if self.role in ('special-year-directory','special-quota') and self.derivedFrom is not None:
            if self.period!='target-year' or self.url is not None or not self.derivedFrom or self.derivedFrom==self.id:raise ValueError('invalid annual derived source')
            return self
        if self.role in ('household-ytd','special-issuance','tier1-prices') and self.derivedFrom is not None:
            if self.period!='latest-completed-month' or self.url is not None or not self.derivedFrom or self.derivedFrom==self.id:
                raise ValueError('derived source required')
            return self
        if self.role=='property-report':
            if self.period!='latest-completed-quarter' or self.url is not None or not self.derivedFrom or self.derivedFrom==self.id:
                raise ValueError('derived source required')
            return self
        if self.derivedFrom is not None or type(self.url) is not str:raise ValueError('invalid static source')
        if self.role in ('tier1-directory','tier1-directory-page'):
            from app.macro.tier1_housing_discovery import ROOT,PAGE
            if self.period!='latest' or self.url!=(ROOT if self.role=='tier1-directory' else PAGE):raise ValueError('invalid housing directory')
            return self
        if self.role in ('property-directory','household-directory'):
            if self.period!='latest' or self.url!='https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html':raise ValueError('invalid directory')
            return self
        if self.role=='special-directory':
            if self.period!='latest' or self.url!='https://zwgls.mof.gov.cn/tjsj/':raise ValueError('invalid special directory')
            return self
        if self.role=='special-quota':
            if re.fullmatch('[0-9]{4}',self.period) is None or not 1000<=int(self.period)<=9999:raise ValueError('invalid year')
            pattern=r'https://yss\.mof\.gov\.cn/'+self.period+r'zyczys/[0-9]{6}/t[0-9]{8}_[0-9]{6,12}\.htm'
        else:
            if re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])',self.period) is None:raise ValueError('invalid month')
            date.fromisoformat(self.period+'-01')
            patterns={
                'household-ytd':r'https://www\.pbc\.gov\.cn/diaochatongjisi/116219/116225/[0-9]{17,22}/index\.html',
                'tier1-prices':r'https://www\.stats\.gov\.cn/xxgk/jd/sjjd2020/[0-9]{6}/t[0-9]{8}_[0-9]{6,12}\.html',
                'special-issuance':r'https://zwgls\.mof\.gov\.cn/tjsj/[0-9]{6}/t[0-9]{8}_[0-9]{6,12}\.htm',
                'special-briefing':r'https://www\.mof\.gov\.cn/zhengwuxinxi/caizhengxinwen/[0-9]{6}/t[0-9]{8}_[0-9]{6,12}\.htm',
            }
            pattern=patterns[self.role]
        if re.fullmatch(pattern,self.url) is None:raise ValueError('invalid official URL')
        return self


class OfficialMacroPolicy(StrictModel):
    collectionEnabled: bool
    schedule: Schedule
    lookbackPeriods: int = Field(ge=1, le=48)
    quoteSide: None
    quarterFallbacks: int = Field(default=0, ge=0, le=1)
    limits: OfficialMacroLimits
    documents: list[OfficialMacroDocument] = Field(min_length=1,max_length=5)

    @model_validator(mode='after')
    def bounded_unique_sources(self):
        urls=[d.url for d in self.documents if d.url is not None]
        if (len({d.id for d in self.documents})!=len(self.documents) or len(urls)!=len(set(urls))
            or self.limits.maxHttpPerPeriod!=len(self.documents) or self.limits.maxHttpPerRun>self.limits.maxHttpPerPeriod):
            raise ValueError('invalid independent budget or duplicate source')
        return self


OFFICIAL_MACRO_GROUPS={'householdCredit','tier1Housing','specialBonds','propertyLoans'}


def validate_official_macro_group(group_id,policy):
    docs=policy.documents;roles=[d.role for d in docs]
    def month(period):
        year,number=map(int,period.split('-'));return year*12+number-1
    if group_id!='propertyLoans' and policy.quarterFallbacks!=0:raise ValueError('invalid fallback group')
    if group_id=='householdCredit':
        if 'household-directory' in roles:
            if (roles!=['household-directory','household-ytd'] or docs[1].derivedFrom!=docs[0].id):
                raise ValueError('invalid household directory graph')
            return policy
        periods=sorted(d.period for d in docs)
        if (not 1<=len(docs)<=2 or set(roles)!={'household-ytd'} or len(set(periods))!=len(periods)
            or (len(periods)==2 and month(periods[1])-month(periods[0])!=1)):raise ValueError('invalid household source periods')
    elif group_id=='tier1Housing':
        if 'tier1-directory' in roles:
            if roles!=['tier1-directory','tier1-directory-page','tier1-prices'] or docs[2].derivedFrom!=docs[1].id:raise ValueError('invalid housing graph')
            return policy
        if roles!=['tier1-prices'] or docs[0].derivedFrom is not None:raise ValueError('invalid housing sources')
    elif group_id=='specialBonds':
        if 'special-budget-directory' in roles:
            if (roles!=['special-budget-directory','special-year-directory','special-quota','special-directory','special-issuance']
                or docs[1].derivedFrom!=docs[0].id or docs[2].derivedFrom!=docs[1].id or docs[4].derivedFrom!=docs[3].id
                or policy.limits.maxHttpPerRun>3):raise ValueError('invalid annual graph')
            return policy
        if 'special-directory' in roles:
            if roles!=['special-directory','special-issuance','special-quota'] or docs[1].derivedFrom!=docs[0].id:
                raise ValueError('invalid special directory graph')
            return policy
        if (not {'special-quota','special-briefing'}<=set(roles)<= {'special-quota','special-briefing','special-issuance'}
            or len(roles)!=len(set(roles))):raise ValueError('invalid special bond sources')
        quota=next(d for d in docs if d.role=='special-quota')
        monthly=[d for d in docs if d.role!='special-quota']
        if (any(d.period[:4]!=quota.period for d in monthly)
            or (len(monthly)==2 and abs(month(monthly[0].period)-month(monthly[1].period))>1)):
            raise ValueError('incompatible annual quota or issuance periods')
    elif group_id=='propertyLoans':
        if sorted(roles)!=['property-directory','property-report']:raise ValueError('invalid property graph')
        directory=next(d for d in docs if d.role=='property-directory');report=next(d for d in docs if d.role=='property-report')
        if report.derivedFrom!=directory.id:raise ValueError('unbound property report')
    else:raise ValueError('unknown official group')
    return policy


def validate_group(group_id: str, raw: object) -> GroupPolicy | FiscalPolicy | OfficialMacroPolicy | ExchangeSummaryPolicy:
    try:
        if group_id in EXCHANGE_SUMMARY_GROUPS:
            return validate_exchange_policy(group_id,raw)
        if group_id in OFFICIAL_MACRO_GROUPS:
            return validate_official_macro_group(group_id,OfficialMacroPolicy.model_validate(raw))
        if group_id == 'governmentBonds':
            return FiscalPolicy.model_validate(raw)
        group = GroupPolicy.model_validate(raw)
        if group_id == 'usdcnh':
            if group.quoteSide is None:
                raise ValueError()
        elif group_id == 'nominalGdp':
            if group.quoteSide is not None or group.lookbackPeriods > 64 or group.limits.maxResponseRows > 64:
                raise ValueError()
        elif group_id == 'vix':
            if group.quoteSide is not None:
                raise ValueError()
        else:
            raise ValueError()
        return group
    except (ValueError, TypeError, KeyError):
        raise ExtensionError('EXTENSION_CONFIG_INVALID') from None


from app.macro.housing_history_plan import HistoryPolicy


class ExtensionConfig(StrictModel):
    schemaVersion: int = Field(ge=1, le=1)
    groups: dict[str, GroupPolicy | FiscalPolicy | OfficialMacroPolicy | ExchangeSummaryPolicy]
    contractSamples: dict[Literal['szMarket'], SzMarketSamplePolicy] = Field(default_factory=dict)
    historyBackfills: dict[Literal['tier1Housing'], HistoryPolicy] = Field(default_factory=dict)

    @model_validator(mode='after')
    def closed_groups(self):
        if not {'usdcnh', 'nominalGdp'} <= set(self.groups) <= {'usdcnh', 'nominalGdp', 'vix', 'governmentBonds'} | OFFICIAL_MACRO_GROUPS | EXCHANGE_SUMMARY_GROUPS:
            raise ValueError('invalid groups')
        for key, group in self.groups.items():
            validate_group(key, group.model_dump())
        return self


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def load_extension_config(path: Path | None = None) -> ExtensionConfig:
    try:
        file = path if path is not None else DEFAULT_EXTENSION_CONFIG_FILE
        with file.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=_unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        return ExtensionConfig.model_validate(value)
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ExtensionError('EXTENSION_CONFIG_INVALID') from None


def selected_extension_groups(config: ExtensionConfig, group_ids=None):
    if group_ids is None:
        return list(config.groups)
    if (type(group_ids) is not list or not group_ids or len(group_ids) > len(config.groups)
        or any(type(key) is not str or key not in config.groups for key in group_ids)
        or len(set(group_ids)) != len(group_ids)):
        raise ExtensionError('EXTENSION_CONFIG_INVALID')
    return group_ids
