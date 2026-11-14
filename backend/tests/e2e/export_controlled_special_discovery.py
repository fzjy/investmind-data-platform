"""专项债发现纯受控合同，不读运行配置/数据库/网络。"""
from copy import deepcopy
from datetime import timedelta
import json
from app.macro.official_macro_store import normalize_document,run_key
from app.macro.official_macro_view import build_official_macro_view
from backend.tests.e2e.export_controlled_official_macro import cases as existing
from backend.tests.macro.test_special_discovery import plan,directory,REPORT
from backend.tests.macro.test_official_special_bonds import monthly,quota
from backend.tests.macro.test_official_macro_plan import NOW


def cases():
    legacy=existing()['full'];result={'legacy':legacy};when=NOW+timedelta(days=14)
    for found in (True,False):
        groups={k:{key:deepcopy(v) for key,v in g.items() if key not in ('series','progress')}
            for k,g in legacy['groups'].items()}
        p=plan(when);docs=[]
        for i,doc in enumerate(p['documents'] if found else p['documents'][:1]):
            if doc['derivedFrom'] is None:
                request=dict(url=doc['url'],period=doc['period'],directoryRunId=None,directoryRawSha256=None)
            else:request=dict(url=REPORT,period='2026-10',directoryRunId=docs[0]['runId'],directoryRawSha256=docs[0]['normalized']['rawSha256'])
            body=directory([(REPORT,'2026年10月地方政府债券发行和债务余额情况')] if found else []) if i==0 else monthly(month=10,ytd='40000',direct='4392') if i==1 else quota()
            docs.append(dict(runId=run_key(p,doc),documentId=doc['id'],request=request,
                retrievedAt=(when+timedelta(seconds=i*2)).isoformat(),completedAt=(when+timedelta(seconds=i*2+1)).isoformat(),
                normalized=normalize_document(p,doc,request,body)))
        group=dict(plan=p,documents=docs,latestAttempt=dict(plan=p,status='completed' if found else 'source_pending',
            errorCode=None,startedAt=when.isoformat(),finishedAt=(when+timedelta(seconds=len(docs)*2)).isoformat()))
        groups['specialBonds']=group
        result['full' if found else 'noCandidate']=build_official_macro_view(dict(analysisDate='2026-11-08',groups=groups))
        if found:
            group['history']=[deepcopy(legacy['groups']['specialBonds'])]
            result['withHistory']=build_official_macro_view(dict(analysisDate='2026-11-08',groups=groups))
    return result


if __name__=='__main__':print(json.dumps(cases(),ensure_ascii=True,allow_nan=False))
