"""银行请求与保守来源投影；不消费未核准的人工表或自报bank字段。"""
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,field_validator,model_validator
from app.stocks.bank_scoring import COMPONENTS,number,normalize_settings,rules,score_bank

class BankCondition(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    field:str
    operator:Literal['gt','lt','gte','lte']
    value:float

    @field_validator('field')
    @classmethod
    def known_field(cls,value):
        if value not in {f['field'] for f in rules()['fields']}:raise ValueError('未知银行筛选字段')
        return value

class BankRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True,allow_inf_nan=False)
    divisor:float=Field(default_factory=lambda:rules()['presets']['defensive']['divisor'])
    weights:dict[str,float]=Field(default_factory=lambda:rules()['presets']['defensive']['weights'].copy())
    conditions:list[BankCondition]=Field(default_factory=list,max_length=20)
    sort:Literal['code','score-desc']='code'

    @model_validator(mode='after')
    def normalize(self):
        if len({c.field for c in self.conditions})!=len(self.conditions):raise ValueError('重复银行筛选字段')
        normalized=normalize_settings(self.model_dump())
        self.weights=normalized['weights']
        return self

def project_bank(stock,as_of,settings):
    """当前发布仅PB及既有合格研究值；没有正式银行供给合同的值一律留空。"""
    inputs={k:None for k in ('revisedYield','revisedGrowth','pb','offensivePbScore','largeBankScore','nim','yearEndNim','provision','yearEndProvision')}
    pb=number(stock.get('pb'));inputs['pb']=float(pb) if pb is not None and pb>0 else None
    fields={f['field']:None for f in rules()['fields']}
    fields['pb']=inputs['pb']
    roe=number(stock.get('roe'));fields['roe']=float(roe) if roe is not None else None
    research=stock.get('researchValues') if isinstance(stock.get('researchValues'),dict) else {}
    for key in ('dividendYield','expectedDividendYield'):
        value=number(research.get(key));fields[key]=float(value) if value is not None else None
    sources={k:'缺少正式统一银行批量输入合同，不使用未核准原站人工表' for k in inputs}
    sources['pb']='当前发布快照PB；业务日'+as_of[:10]
    sources['revisedYield']='缺修正预期每股分红/价格基准/预测期间的核准来源'
    sources['revisedGrowth']='缺修正预期增长的统一来源与规则版本'
    sources['largeBankScore']='缺核准银行分级来源，不按名称或市值推断'
    sources['nim']=sources['yearEndNim']='缺同口径本期与上年末净息差来源'
    sources['provision']=sources['yearEndProvision']='缺同口径本期与上年末拨备覆盖率来源'
    sources['roe']='原发布报告ROE；未核验为TTM、加权或银行专属评分'
    sources['ahDiscount']='缺同日A/H两腿价格、映射及CNY/HKD汇率合同'
    scored=score_bank(inputs,settings)
    return dict(algorithmVersion=rules()['algorithmVersion'],asOf=as_of,inputs=inputs,fields=fields,
        sources=sources,ahDiscount=None,**scored)

def bank_matches(evaluation,conditions):
    for condition in conditions:
        value=evaluation['fields'][condition.field]
        if value is None:return False
        expected=condition.value
        if not {'gt':value>expected,'lt':value<expected,'gte':value>=expected,'lte':value<=expected}[condition.operator]:return False
    return True
