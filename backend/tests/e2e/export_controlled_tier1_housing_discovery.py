"""一线房价发现跨语言夹具；纯受控原件，无网络和业务库。"""
from copy import deepcopy
from datetime import timedelta
import json

from app.macro.official_macro_store import normalize_document,run_key
from app.macro.official_macro_view import build_official_macro_view
from app.macro.tier1_housing_discovery import select_tier1_report
from backend.tests.e2e.export_controlled_official_macro import cases as legacy
from backend.tests.macro.test_tier1_housing_discovery import plan,directory,REPORT,LABEL,NOW,document


def cases():
    result={'legacy':legacy()['full']};p=plan()
    for found in (None,0,1):
        groups={g:{k:deepcopy(v) for k,v in value.items() if k not in ('series','progress')} for g,value in legacy()['full']['groups'].items()}
        docs=[]
        bodies=[directory([(REPORT,LABEL)] if found==i else [],i) for i in range(2)]+([] if found is None else [document()])
        for i,(doc,body) in enumerate(zip(p['documents'],bodies)):
            req=dict(url=doc['url'],period=doc['period'],directoryRunId=None,directoryRawSha256=None)
            if i==2:
                chosen=select_tier1_report([v['normalized'] for v in docs]);parent=docs[1]
                req=dict(url=chosen['url'],period=chosen['period'],directoryRunId=parent['runId'],directoryRawSha256=parent['normalized']['rawSha256'])
            docs.append(dict(runId=run_key(p,doc),documentId=doc['id'],request=req,retrievedAt=(NOW+timedelta(seconds=i*3)).isoformat(),
                completedAt=(NOW+timedelta(seconds=i*3+1)).isoformat(),normalized=normalize_document(p,doc,req,body)))
        groups['tier1Housing']=dict(plan=p,documents=docs,latestAttempt=dict(plan=p,status='source_pending' if found is None else 'completed',
            errorCode=None,startedAt=NOW.isoformat(),finishedAt=(NOW+timedelta(seconds=len(docs)*3)).isoformat()))
        result['missing' if found is None else f'page{found}']=build_official_macro_view(dict(analysisDate='2026-10-25',groups=groups))
    # 下一个真实周的受控夹具：保留上一月两房型的整组证据，而非搬值补新月。
    now=NOW+timedelta(days=14);p=plan(now);g=deepcopy(result['page0']['groups']['tier1Housing'])
    label=LABEL.replace('9月份','10月份')
    bodies=[directory([(REPORT,label)],0),directory(page=1),document(new='上涨0.2%',title='2026年10月份商品住宅销售价格变动情况解读')]
    docs=[]
    for i,(doc,body) in enumerate(zip(p['documents'],bodies)):
        req=dict(url=doc['url'],period=doc['period'],directoryRunId=None,directoryRawSha256=None)
        if i==2:
            chosen=select_tier1_report([v['normalized'] for v in docs]);parent=docs[1]
            req=dict(url=chosen['url'],period=chosen['period'],directoryRunId=parent['runId'],directoryRawSha256=parent['normalized']['rawSha256'])
        docs.append(dict(runId=run_key(p,doc),documentId=doc['id'],request=req,retrievedAt=(now+timedelta(seconds=i*3)).isoformat(),
            completedAt=(now+timedelta(seconds=i*3+1)).isoformat(),normalized=normalize_document(p,doc,req,body)))
    groups={key:dict(plan=None,documents=[],latestAttempt=None) for key in legacy()['full']['groups']}
    groups['tier1Housing']=dict(plan=p,documents=docs,history=[g],latestAttempt=dict(plan=p,status='completed',errorCode=None,
        startedAt=now.isoformat(),finishedAt=(now+timedelta(seconds=9)).isoformat()))
    result['history']=build_official_macro_view(dict(analysisDate='2026-11-08',groups=groups))
    for name,old in [('static',legacy()['full']['groups']['tier1Housing']),('discovery',g)]:
        missing=deepcopy(groups)
        mg=missing['tier1Housing'];mg['documents']=mg['documents'][:2];mg['history']=[deepcopy(old)]
        mg['latestAttempt']['status']='source_pending'
        for i,d in enumerate(mg['documents']):
            d['normalized']=normalize_document(p,p['documents'][i],d['request'],directory(page=i))
        result['missing-history-'+name]=build_official_macro_view(dict(analysisDate='2026-11-08',groups=missing))
    return result


if __name__=='__main__':print(json.dumps(cases(),ensure_ascii=True,allow_nan=False))
