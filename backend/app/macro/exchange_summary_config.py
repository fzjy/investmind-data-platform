"""交易所聚合策略；只允许具名沪深分母和三所余额，不允许逐股/任意查询。"""
import re
from typing import Literal

from pydantic import Field,model_validator

from app.macro.extensions.policy_types import Schedule,StrictModel

EXCHANGE_SUMMARY_GROUPS=frozenset(('marginBalances','aShareFloatMv'))
CONTRACTS={
    'marginBalances':{'apiName':'margin','fields':['trade_date','exchange_id','rzye'],'unit':'元',
        'documentationUrl':'https://tushare.pro/document/2?doc_id=58',
        'params':[{'exchange_id':e} for e in ('SSE','SZSE','BSE')]},
    'aShareFloatMv':{'apiName':'daily_info','fields':['trade_date','ts_code','exchange','float_mv'],'unit':'亿元',
        'documentationUrl':'https://tushare.pro/document/2?doc_id=215',
        'params':[{'ts_code':'SH_A','exchange':'SH'},{'ts_code':'SZ_A','exchange':'SZ'}]},
}


class ExchangeSummaryLimits(StrictModel):
    maxHttpPerRun:int=Field(ge=1,le=3)
    maxHttpPerPeriod:int=Field(ge=1,le=3)
    requestsPerMinute:int=Field(ge=1,le=10)
    requestTimeoutSeconds:int=Field(ge=1,le=20)
    maxRetries:int=Field(ge=0,le=0)
    maxResponseBytes:int=Field(ge=1,le=1048576)
    maxResponseRows:int=Field(ge=1,le=1)


class ExchangeSummaryRequest(StrictModel):
    id:str
    params:dict[str,str]

    @model_validator(mode='after')
    def valid_id(self):
        if re.fullmatch(r'[a-z][a-z0-9-]{0,47}',self.id) is None:
            raise ValueError('invalid request identity')
        return self


class ExchangeSummaryPolicy(StrictModel):
    collectionEnabled:bool
    schedule:Schedule
    lookbackPeriods:int=Field(ge=1,le=48)
    quoteSide:None
    publicationLagTradingDays:int=Field(ge=1,le=3)
    publicationTime:str
    publicationTimezone:Literal['Asia/Shanghai']
    calendarLookbackDays:int=Field(ge=1,le=90)
    calendarLookaheadDays:int=Field(ge=1,le=31)
    calendarExchanges:list[Literal['SSE','SZSE']]
    providerUrl:Literal['https://api.tushare.pro']
    documentationUrl:str
    useScope:Literal['personal-local-noncommercial']
    apiName:Literal['margin','daily_info']
    fields:list[str]
    unit:Literal['元','亿元']
    requests:list[ExchangeSummaryRequest]=Field(min_length=1,max_length=3)
    limits:ExchangeSummaryLimits

    @model_validator(mode='after')
    def valid_independent_controls(self):
        if (re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]',self.publicationTime) is None
            or len(self.calendarExchanges)!=2 or set(self.calendarExchanges)!={'SSE','SZSE'}
            or len({r.id for r in self.requests})!=len(self.requests)
            or len(set(self.fields))!=len(self.fields)
            or self.limits.maxHttpPerPeriod!=len(self.requests)
            or self.limits.maxHttpPerRun>self.limits.maxHttpPerPeriod):
            raise ValueError('invalid independent controls')
        return self


def validate_exchange_policy(group_id:str,value:object)->ExchangeSummaryPolicy:
    if group_id not in EXCHANGE_SUMMARY_GROUPS:
        raise ValueError('unknown exchange group')
    policy=ExchangeSummaryPolicy.model_validate(value)
    contract=CONTRACTS[group_id]
    if (policy.apiName!=contract['apiName'] or policy.unit!=contract['unit']
        or policy.documentationUrl!=contract['documentationUrl']
        or set(policy.fields)!=set(contract['fields'])
        or [r.params for r in policy.requests]!=contract['params']):
        raise ValueError('invalid exchange scope')
    return policy
