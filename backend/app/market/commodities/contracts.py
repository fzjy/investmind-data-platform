import hashlib
import json
import re
from datetime import timedelta
from typing import Literal

from pydantic import Field, model_validator
from app.market.data.contracts import StrictModel

JOB_NAME='sync-commodities'
FIELDS={
    'fut_trade_cal':('exchange','cal_date','is_open'),
    'fut_basic':('ts_code','fut_code','exchange','name','trade_unit','quote_unit','per_unit','list_date','delist_date'),
    'fut_mapping':('ts_code','trade_date','mapping_ts_code'),
    'fut_daily':('ts_code','trade_date','pre_close','pre_settle','open','high','low','close','settle','vol','amount','oi'),
}
IDENTITIES=(('AU.SHF','SHFE','precious','黄金'),('AG.SHF','SHFE','precious','白银'),
    ('CU.SHF','SHFE','nonferrous','沪铜'),('AL.SHF','SHFE','nonferrous','沪铝'),
    ('RB.SHF','SHFE','ferrous','螺纹钢'),('I.DCE','DCE','ferrous','铁矿石'),
    ('SC.INE','INE','energy','原油'),('TA.ZCE','CZCE','energy','PTA'),
    ('M.DCE','DCE','agriculture','豆粕'),('P.DCE','DCE','agriculture','棕榈油'))


class CommodityAsset(StrictModel):
    code: str = Field(strict=True,min_length=1)
    exchange: str = Field(strict=True)
    category: Literal['precious','nonferrous','ferrous','energy','agriculture']
    name: str = Field(strict=True,min_length=1)


class CommodityOptions(StrictModel):
    enabled: bool = Field(default=False,strict=True)
    history_days: int = Field(default=120,strict=True,ge=5,le=366)
    max_requests_per_run: int = Field(default=60,strict=True,ge=1,le=60)
    max_retries: int = Field(default=0,strict=True,ge=0,le=0)
    timeout_seconds: int = Field(default=20,strict=True,ge=1,le=60)
    assets: tuple[CommodityAsset,...] = tuple(CommodityAsset(code=c,exchange=e,category=g,name=n) for c,e,g,n in IDENTITIES)

    @model_validator(mode='after')
    def finite_scope(self):
        if len(self.assets)!=10 or {(a.code,a.exchange,a.category) for a in self.assets}!={(c,e,g) for c,e,g,_ in IDENTITIES}:
            raise ValueError('COMMODITY_FIXED_SCOPE_REQUIRED')
        return self


def fingerprint(catalog):
    data={'version':1,'assets':[a.model_dump(mode='json') for a in catalog.commodities.assets],
        'historyDays':catalog.commodities.history_days,'fields':FIELDS}
    return hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def window(catalog,day):
    return day-timedelta(days=catalog.commodities.history_days-1)


def monthly_code(code,asset):
    product,suffix=asset.code.split('.')
    return isinstance(code,str) and re.fullmatch(re.escape(product)+r'\d{3,4}\.'+re.escape(suffix),code) is not None


def request(api,params,catalog,day):
    if api not in FIELDS: raise ValueError('COMMODITY_API_INVALID')
    identity=hashlib.sha256(json.dumps([fingerprint(catalog),api,params,day.isoformat()],sort_keys=True).encode()).hexdigest()
    return {'api':api,'params':params,'identity':identity,'asOf':day.isoformat(),'startDate':window(catalog,day).isoformat()}


def source_requests(catalog,day):
    dates={'start_date':window(catalog,day).strftime('%Y%m%d'),'end_date':day.strftime('%Y%m%d')}
    result=[request('fut_trade_cal',{'exchange':exchange,**dates},catalog,day)
        for exchange in sorted({a.exchange for a in catalog.commodities.assets})]
    for asset in catalog.commodities.assets:
        result.append(request('fut_basic',{'exchange':asset.exchange,'fut_code':asset.code.split('.')[0],'fut_type':'1'},catalog,day))
        result.append(request('fut_mapping',{'ts_code':asset.code,**dates},catalog,day))
    return result
