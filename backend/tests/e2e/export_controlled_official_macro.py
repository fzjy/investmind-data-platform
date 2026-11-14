"""受控跨语言合同，不连接数据库或读取用户配置。"""
from datetime import timedelta
import json

from app.macro.official_macro_normalize import parse_household_ytd,parse_tier1_housing,parse_special_quota,parse_special_issuance
from app.macro.official_macro_plan import build_official_macro_plan
from app.macro.official_macro_store import run_key
from app.macro.official_macro_view import build_official_macro_view,SHAPES,SOURCE,SERIES
from backend.tests.macro.test_official_macro_config import policy
from backend.tests.macro.test_official_macro_plan import NOW
from backend.tests.macro.test_official_household_credit import document
from backend.tests.macro.test_official_tier1_housing import document as housing
from backend.tests.macro.test_official_special_bonds import quota,monthly,actual_briefing


def cases():
    groups={g:{'plan':None,'documents':[],'latestAttempt':None} for g in SERIES}
    for group,parsed in {
        'householdCredit':[parse_household_ytd(document(amount='1.00',title='2026年前八个月金融统计数据报告'),'2026-08'),parse_household_ytd(document(),'2026-09')],
        'tier1Housing':[parse_tier1_housing(housing(),'2026-09')],
        'specialBonds':[parse_special_quota(quota(),2026),parse_special_issuance(monthly(),'2026-08','special-issuance'),parse_special_issuance(actual_briefing(),'2026-09','special-briefing')],
    }.items():
        p=build_official_macro_plan(group,policy(group),NOW)
        docs=[]
        for doc,value in zip(p['documents'],parsed,strict=True):
            docs.append({'runId':run_key(p,doc),'documentId':doc['id'],
                'request':{'url':doc['url'],'period':doc['period'],'directoryRunId':None,'directoryRawSha256':None},
                'retrievedAt':NOW.isoformat(),'completedAt':(NOW+timedelta(seconds=1)).isoformat(),'normalized':value})
        groups[group]={'plan':p,'documents':docs,'latestAttempt':{'plan':p,'status':'completed','errorCode':None,
            'startedAt':NOW.isoformat(),'finishedAt':(NOW+timedelta(seconds=1)).isoformat()}}
    p=build_official_macro_plan('propertyLoans',policy('propertyLoans'),NOW)
    groups['propertyLoans']={'plan':p,'documents':[],'latestAttempt':{'plan':p,'status':'failed','errorCode':'EXTENSION_AUTH_ERROR',
        'startedAt':NOW.isoformat(),'finishedAt':(NOW+timedelta(seconds=1)).isoformat()}}
    return {'full':build_official_macro_view({'analysisDate':'2026-10-25','groups':groups})}


if __name__=='__main__':
    print(json.dumps(cases(),ensure_ascii=True,allow_nan=False))
