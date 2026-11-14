"""历史报告合同跨语言夹具，不请求真实网络。"""
from copy import deepcopy
from datetime import timedelta
import json
from backend.tests.e2e.export_controlled_tier1_housing_discovery import cases as ordinary
from backend.tests.macro.test_housing_history import policy
from backend.tests.macro.test_tier1_housing_discovery import directory,REPORT,LABEL,NOW,document
from app.macro.housing_history_plan import build_directory_plan,build_report_plan
from app.macro.official_macro_store import normalize_document,run_key
from app.macro.official_macro_view import build_official_macro_view


def recovery_proof(plan,doc,failed,started):
    from app.db.models.ops import JobRun
    from app.db.models.raw import IngestionBatch
    from app.macro.official_macro_store import job_name,dataset_name
    from app.macro.housing_history_recovery import public_recovery
    key=run_key(plan,doc)
    run=JobRun(id=key,job_name=job_name(plan),business_key=plan['groupId']+':'+plan['scheduledAt']+':'+doc['id'],status='failed',started_at=failed,finished_at=failed,
        request_count=1,row_count=0,error_message='EXTENSION_TIMEOUT',details=dict(schemaVersion=1,plan=plan,document=doc,
        request=dict(url=doc['url'],period=doc['period'],directoryRunId=None,directoryRawSha256=None),phase='failed',rawId=None,normalized=None,coreBindings=[]))
    batch=IngestionBatch(id=key,source='official',dataset=dataset_name(plan),mode='live',business_date=None,status='failed',started_at=failed,finished_at=failed,request_count=1,row_count=0,rejected_count=0)
    return public_recovery(run,batch,started)


def cases():
    base=ordinary()['missing-history-static'];now=NOW+timedelta(days=14,seconds=20)
    p=build_directory_plan(policy(),now,'2022-11-01','2026-10-01',['2026-09']);proof=[]
    for i,doc in enumerate(p['documents']):
        request=dict(url=doc['url'],period=doc['period'],directoryRunId=None,directoryRawSha256=None)
        body=directory([(REPORT,LABEL.replace('9月份','8月份'))] if i==0 else [],i)
        proof.append(dict(runId=run_key(p,doc),documentId=doc['id'],request=request,retrievedAt=(now+timedelta(seconds=i*3)).isoformat(),
            completedAt=(now+timedelta(seconds=i*3+1)).isoformat(),normalized=normalize_document(p,doc,request,body)))
    rp=build_report_plan(p,proof);doc=rp['documents'][0];request=dict(url=doc['url'],period=doc['period'],directoryRunId=None,directoryRawSha256=None)
    report=dict(runId=run_key(rp,doc),documentId=doc['id'],request=request,retrievedAt=(now+timedelta(seconds=7)).isoformat(),
        completedAt=(now+timedelta(seconds=8)).isoformat(),normalized=normalize_document(rp,doc,request,document(title='2026年8月份商品住宅销售价格变动情况解读')))
    blank={k:dict(plan=None,documents=[],latestAttempt=None) for k in base['groups']}
    blank['tier1Housing']=dict(plan=rp,documents=[report],latestAttempt=dict(plan=rp,status='completed',errorCode=None,
        startedAt=(now+timedelta(seconds=6)).isoformat(),finishedAt=(now+timedelta(seconds=8)).isoformat()))
    standalone=build_official_macro_view(dict(analysisDate='2026-11-08',groups=blank))
    groups={k:{n:deepcopy(v) for n,v in g.items() if n not in ('series','progress')} for k,g in base['groups'].items()}
    groups['tier1Housing']['history'].append(standalone['groups']['tier1Housing'])
    result=dict(standalone=standalone,mixed=build_official_macro_view(dict(analysisDate='2026-11-08',groups=groups)))
    from backend.tests.e2e.export_controlled_official_macro import cases as legacy
    groups={k:{n:deepcopy(v) for n,v in g.items() if n not in ('series','progress')} for k,g in legacy()['full']['groups'].items()}
    groups['tier1Housing']['history']=[standalone['groups']['tier1Housing']]
    result['legacy-mixed']=build_official_macro_view(dict(analysisDate='2026-11-08',groups=groups))
    from app.macro.housing_history_recovery import recovery_key
    recovered=deepcopy(proof)
    parent=recovered[0]['runId'];recovered[0]['runId']=recovery_key(parent)
    recovered[0]['recovery']=recovery_proof(p,p['documents'][0],now,now)
    recovered_plan=build_report_plan(p,recovered);recovered_report=deepcopy(report)
    parent=run_key(recovered_plan,recovered_plan['documents'][0]);recovered_report['runId']=recovery_key(parent)
    recovered_report['recovery']=recovery_proof(recovered_plan,recovered_plan['documents'][0],now+timedelta(seconds=5),now+timedelta(seconds=6))
    blank['tier1Housing']=dict(plan=recovered_plan,documents=[recovered_report],latestAttempt=dict(plan=recovered_plan,status='completed',errorCode=None,
        startedAt=(now+timedelta(seconds=6)).isoformat(),finishedAt=(now+timedelta(seconds=8)).isoformat()))
    result['recovered']=build_official_macro_view(dict(analysisDate='2026-11-08',groups=blank))
    return result


if __name__=='__main__':print(json.dumps(cases(),ensure_ascii=True,allow_nan=False))
