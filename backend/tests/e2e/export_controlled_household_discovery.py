"""纯受控目录合同，不采集、不读真实配置或数据库。"""
from copy import deepcopy
from datetime import datetime,timedelta
import json

from app.macro.official_macro_store import normalize_document,run_key
from app.macro.official_macro_view import build_official_macro_view
from backend.tests.e2e.export_controlled_official_macro import cases as existing_cases
from backend.tests.macro.test_household_discovery import plan,directory,REPORT
from backend.tests.macro.test_official_household_credit import document
from backend.tests.macro.test_official_macro_plan import NOW


def cases():
    result={}
    for found in (False,True):
        groups=deepcopy(existing_cases()['full']['groups'])
        groups={key:{k:v for k,v in g.items() if k not in ('series','progress')} for key,g in groups.items()}
        p=plan();docs=[]
        first=p['documents'][0]
        request=dict(url=first['url'],period=first['period'],directoryRunId=None,directoryRawSha256=None)
        normalized=normalize_document(p,first,request,directory([(REPORT,'2026年前三季度金融统计数据报告')] if found else []))
        docs.append(dict(runId=run_key(p,first),documentId=first['id'],request=request,
            retrievedAt=NOW.isoformat(),completedAt=(NOW+timedelta(seconds=1)).isoformat(),normalized=normalized))
        if found:
            second=p['documents'][1]
            request=dict(url=REPORT,period='2026-09',directoryRunId=docs[0]['runId'],directoryRawSha256=normalized['rawSha256'])
            docs.append(dict(runId=run_key(p,second),documentId=second['id'],request=request,
                retrievedAt=(NOW+timedelta(seconds=2)).isoformat(),completedAt=(NOW+timedelta(seconds=3)).isoformat(),
                normalized=normalize_document(p,second,request,document())))
        groups['householdCredit']=dict(plan=p,documents=docs,latestAttempt=dict(plan=p,
            status='completed' if found else 'source_pending',errorCode=None,startedAt=NOW.isoformat(),
            finishedAt=(NOW+timedelta(seconds=3)).isoformat()))
        result['full' if found else 'noCandidate']=build_official_macro_view(dict(analysisDate='2026-10-25',groups=groups))
    groups={key:{k:v for k,v in g.items() if k not in ('series','progress')} for key,g in deepcopy(result['full']['groups']).items()}
    old=deepcopy(groups['householdCredit']);old_now=NOW-timedelta(days=28);old_plan=plan(now=old_now)
    old['plan']=old_plan
    first,second=old_plan['documents'];source='https://www.pbc.gov.cn/diaochatongjisi/116219/116225/2026091417020862747/index.html'
    request=dict(url=first['url'],period=first['period'],directoryRunId=None,directoryRawSha256=None)
    normalized=normalize_document(old_plan,first,request,directory([(source,'2026年8月金融统计数据报告')]))
    old['documents']=[dict(runId=run_key(old_plan,first),documentId=first['id'],request=request,
        retrievedAt=old_now.isoformat(),completedAt=(old_now+timedelta(seconds=1)).isoformat(),normalized=normalized)]
    request=dict(url=source,period='2026-08',directoryRunId=old['documents'][0]['runId'],directoryRawSha256=normalized['rawSha256'])
    old['documents'].append(dict(runId=run_key(old_plan,second),documentId=second['id'],request=request,
        retrievedAt=(old_now+timedelta(seconds=2)).isoformat(),completedAt=(old_now+timedelta(seconds=3)).isoformat(),
        normalized=normalize_document(old_plan,second,request,document(title='2026年前八个月金融统计数据报告',amount='1.00'))))
    old['latestAttempt']=dict(plan=old_plan,status='completed',errorCode=None,startedAt=old_now.isoformat(),finishedAt=(old_now+timedelta(seconds=3)).isoformat())
    previous=deepcopy(groups);previous['householdCredit']=old
    groups['householdCredit']['history']=[build_official_macro_view(dict(analysisDate='2026-10-25',groups=previous))['groups']['householdCredit']]
    result['withHistory']=build_official_macro_view(dict(analysisDate='2026-10-25',groups=groups))
    history=[]
    end=2026*12+8
    for offset in range(13,0,-1):
        month_index=end-offset;year,number=divmod(month_index,12);period=f'{year:04}-{number+1:02}'
        check_year,check_month=divmod(month_index+1,12)
        check=datetime(check_year,check_month+1,25,6,tzinfo=NOW.tzinfo)
        hp=plan(now=check);first,second=hp['documents']
        request=dict(url=first['url'],period=first['period'],directoryRunId=None,directoryRawSha256=None)
        normalized=normalize_document(hp,first,request,directory([(source,f'{year}年{number+1}月金融统计数据报告')]))
        hd=[dict(runId=run_key(hp,first),documentId=first['id'],request=request,retrievedAt=check.isoformat(),
            completedAt=(check+timedelta(seconds=1)).isoformat(),normalized=normalized)]
        request=dict(url=source,period=period,directoryRunId=hd[0]['runId'],directoryRawSha256=normalized['rawSha256'])
        hd.append(dict(runId=run_key(hp,second),documentId=second['id'],request=request,retrievedAt=(check+timedelta(seconds=2)).isoformat(),
            completedAt=(check+timedelta(seconds=3)).isoformat(),normalized=normalize_document(hp,second,request,
                document(title=f'{year}年前{number+1}个月金融统计数据报告',amount='1.00'))))
        hg=dict(plan=hp,documents=hd,latestAttempt=dict(plan=hp,status='completed',errorCode=None,
            startedAt=check.isoformat(),finishedAt=(check+timedelta(seconds=3)).isoformat()))
        prior=deepcopy(groups);prior['householdCredit']=hg
        history.append(build_official_macro_view(dict(analysisDate='2026-10-25',groups=prior))['groups']['householdCredit'])
    groups['householdCredit']['history']=history
    result['largeHistory']=build_official_macro_view(dict(analysisDate='2026-10-25',groups=groups))
    return result


if __name__=='__main__':print(json.dumps(cases(),ensure_ascii=True,allow_nan=False))
