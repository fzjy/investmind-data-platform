"""年度发现跨语言纯夹具，无网络/运行配置/业务库。"""
from copy import deepcopy
from datetime import timedelta
import json
from backend.tests.e2e.export_controlled_official_macro import cases as legacy
from backend.tests.macro.test_special_quota_discovery import plan,root_page,year_page,quota,monthly,monthly_directory,REPORT,page,NOW
from app.macro.official_macro_store import normalize_document,run_key
from app.macro.official_macro_view import build_official_macro_view

def cases():
    result={'legacy':legacy()['full']};p=plan()
    for missing in (None,0,1,3):
        groups={g:{k:deepcopy(v) for k,v in value.items() if k not in ('series','progress')} for g,value in legacy()['full']['groups'].items()}
        bodies=[root_page(),year_page(),quota(),monthly_directory([(REPORT,'2026年9月地方政府债券发行和债务余额情况')]),monthly(9,'35608','6339')]
        if missing is not None:bodies[missing]=page('预算报表' if missing==0 else '2026年中央财政预算' if missing==1 else '统计数据');bodies=bodies[:missing+1]
        docs=[]
        for i,(doc,body) in enumerate(zip(p['documents'],bodies)):
            request=dict(url=doc['url'],period=doc['period'],directoryRunId=None,directoryRawSha256=None)
            if doc['derivedFrom']:
                parent=next(v for v in docs if v['documentId']==doc['derivedFrom']);selected=parent['normalized']['selected']
                request=dict(url=selected['url'],period=selected.get('year',selected.get('period')),directoryRunId=parent['runId'],directoryRawSha256=parent['normalized']['rawSha256'])
            docs.append(dict(runId=run_key(p,doc),documentId=doc['id'],request=request,retrievedAt=(NOW+timedelta(seconds=i*3)).isoformat(),
                completedAt=(NOW+timedelta(seconds=i*3+1)).isoformat(),normalized=normalize_document(p,doc,request,body)))
        groups['specialBonds']=dict(plan=p,documents=docs,latestAttempt=dict(plan=p,status='completed' if missing is None else 'source_pending',
            errorCode=None,startedAt=NOW.isoformat(),finishedAt=(NOW+timedelta(seconds=len(docs)*3)).isoformat()))
        result['full' if missing is None else f'missing{missing}']=build_official_macro_view(dict(analysisDate='2026-10-25',groups=groups))
    return result

if __name__=='__main__':print(json.dumps(cases(),ensure_ascii=True,allow_nan=False))
