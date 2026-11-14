"""已研究发行人原件的封闭采样计划；不推导或证明成员身份。"""
from datetime import date
import re
from typing import Literal
from uuid import UUID,uuid5
from zoneinfo import ZoneInfo

from pydantic import model_validator

from app.macro.official_member_policy import StrictPolicy
from app.macro.fund_contract_discovery import BOUNDS
from app.macro.extensions.plan import digest
from app.macro.extensions.store import instant


SOURCES={
    '512800':('html','https://www.fsfund.com/fund/512800/fundDetail.shtml','SH'),
    '159928':('html','https://www.99fund.com/main/products/pofund/159928/fundgk.shtml','SZ'),
    '512010':('pdf','https://cdn.efunds.com.cn/owch/data/bulletin/20250428/易方达沪深300医药卫生交易型开放式指数证券投资基金基金产品资料概要更新.pdf','SH'),
    '512400':('html','https://www.nffund.com/new/personal-financing/detail.html?fundCode=512400','SH'),
    '512980':('html','https://www.gffunds.com.cn/funds//?fundcode=512980','SH'),
    '515030':('pdf','https://fund.chinaamc.com/upload/resources/file/2025/05/29/515030_华夏中证新能源汽车交易型开放式指数证券投资基金招募说明书更新（2025年5月30日公告）.pdf','SH'),
    '512200':('html','https://www.nffund.com/new/personal-financing/detail.html?fundCode=512200','SH'),
    '588200':('pdf','https://www.sse.com.cn/disclosure/fund/announcement/c/new/2024-09-10/588200_20240910_RPWQ.pdf','SH'),
}
NAMESPACE=UUID('64baed7f-a3cb-5d50-a861-b984b2347b0a')


class IssuerSampleSource(StrictPolicy):
    fundCode:str
    mediaType:Literal['html','pdf']
    url:str

    @model_validator(mode='after')
    def known_source(self):
        source=SOURCES.get(self.fundCode)
        if source is None or (self.mediaType,self.url)!=source[:2]:raise ValueError('ISSUER_SAMPLE_SOURCE_INVALID')
        return self


def build_sample_plan(catalog,fund_code,now):
    try:
        doc=next(d for d in catalog.catalog_member_policy.sampleDocuments if d.fundCode==fund_code)
        target=next(t for t in catalog.targets if t.code==fund_code+'.'+SOURCES[fund_code][2])
        value={'schemaVersion':1,'firstBusinessDate':instant(now).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),
            'catalogFingerprint':catalog.fingerprint,'document':doc.model_dump(),
            'target':{'code':target.code,'kind':target.kind,'label':target.label,'sourceIndexNameHint':target.source_index_name_hint},
            'limits':{k:getattr(catalog.official_policy,k) for k in BOUNDS}}
        value['fingerprint']=digest(value);validate_sample_plan(value);return value
    except Exception:raise ValueError('ISSUER_SAMPLE_PLAN_INVALID') from None


def validate_sample_plan(plan):
    try:
        if (type(plan) is not dict or set(plan)!={'schemaVersion','firstBusinessDate','catalogFingerprint','document','target','limits','fingerprint'}
            or type(plan['schemaVersion']) is not int or plan['schemaVersion']!=1
            or type(plan['firstBusinessDate']) is not str or date.fromisoformat(plan['firstBusinessDate']).isoformat()!=plan['firstBusinessDate']
            or type(plan['catalogFingerprint']) is not str or re.fullmatch('[0-9a-f]{64}',plan['catalogFingerprint']) is None):raise ValueError()
        doc=IssuerSampleSource.model_validate(plan['document']);target=plan['target'];limits=plan['limits']
        if (type(target) is not dict or set(target)!={'code','kind','label','sourceIndexNameHint'} or target['kind']!='etf'
            or target['code']!=doc.fundCode+'.'+SOURCES[doc.fundCode][2]
            or type(target['label']) is not str or not 1<=len(target['label'].strip())<=160
            or type(target['sourceIndexNameHint']) is not str or not 1<=len(target['sourceIndexNameHint'].strip())<=100
            or type(limits) is not dict or set(limits)!=set(BOUNDS)):raise ValueError()
        for key,maximum in BOUNDS.items():
            if type(limits[key]) is not int or not (0 if key=='maxRetries' else 1)<=limits[key]<=maximum:raise ValueError()
        if digest({k:v for k,v in plan.items() if k!='fingerprint'})!=plan['fingerprint']:raise ValueError()
        return doc
    except Exception:raise ValueError('ISSUER_SAMPLE_PLAN_INVALID') from None


def sample_run_key(plan):
    return str(uuid5(NAMESPACE,validate_sample_plan(plan).fundCode))
