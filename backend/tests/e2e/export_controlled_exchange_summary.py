"""纯受控跨语言合同；无数据库、供应商或用户密钥读取。"""
from copy import deepcopy
from datetime import timedelta
import json

from app.macro.exchange_summary_normalize import normalize_exchange_response
from app.macro.exchange_summary_store import run_key
from app.macro.exchange_summary_store import safe_business
from app.macro.exchange_summary_view import build_exchange_summary_view
from app.macro.extensions.plan import digest
from backend.tests.macro.test_exchange_summary_plan import build
from backend.tests.macro.test_exchange_summary_store import NOW


def controlled(*,failed_bse=False,amounts=None,other_day=False,lookback=48,at=None):
    from backend.tests.macro.test_exchange_summary_config import policy
    at=at or NOW;latest_at=at
    groups={}
    for group in ('marginBalances','aShareFloatMv'):
        group_at=at+timedelta(days=1) if other_day and group=='aShareFloatMv' else at
        latest_at=max(latest_at,group_at)
        config=policy(group);config['lookbackPeriods']=lookback
        p=build(group_at.isoformat(),group=group,p=config);docs=[];latest=None
        values=(amounts or {}).get(group,['100000000','200000000','999999999'] if group=='marginBalances' else ['10','20'])
        for i,doc in enumerate(p['documents']):
            key=run_key(p,doc);start=group_at+timedelta(seconds=i*3);done=start+timedelta(seconds=2)
            failed=failed_bse and group=='marginBalances' and i==2
            latest={'plan':p,'documentId':doc['id'],'runId':key,'phase':'failed' if failed else 'completed',
                'startedAt':start.isoformat(),'finishedAt':done.isoformat(),'errorCode':'EXTENSION_PERMISSION_ERROR' if failed else None}
            if failed:break
            business={'fields':doc['query']['fields'],'items':[[doc['query']['params'].get(f,values[i]) for f in doc['query']['fields']]]}
            business=safe_business(business,p['limits']['maxResponseBytes'])
            normalized=normalize_exchange_response(p,doc,business);bindings=[]
            for point in normalized['points']:
                bindings.append({'coreId':int(p['targetDate'].replace('-',''))*10+i+1+(3 if group=='aShareFloatMv' else 0),'seriesId':point['seriesId'],'period':point['period'],'value':point['value'],
                    'revision':digest({'runId':key,'businessSha256':digest(business),'point':point})})
            docs.append({'documentId':doc['id'],'runId':key,'retrievedAt':(start+timedelta(seconds=1)).isoformat(),
                'completedAt':done.isoformat(),'businessData':business,'businessSha256':digest(business),'normalized':normalized,'coreBindings':bindings})
        groups[group]={'targets':[{'plan':p,'documents':docs}],'latestAttempt':latest}
    from zoneinfo import ZoneInfo
    return build_exchange_summary_view({'analysisDate':latest_at.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),'groups':groups})


def cases():
    from app.macro.extensions.view import _empty,build_extension_view
    result={'full':controlled(),'bseFailure':controlled(failed_bse=True),'differentDay':controlled(other_day=True),
        'zero':controlled(amounts={'marginBalances':['0','0','0']}),
        'lexical':controlled(amounts={'marginBalances':['1e3','+0001.2300','0'],'aShareFloatMv':['.5','1.0e-2']}),
        'largeInteger':controlled(amounts={'marginBalances':[9007199254740993,'0','0']}),
        'fraction':controlled(amounts={'marginBalances':['1','0','0'],'aShareFloatMv':['3','0']})}
    result['standaloneExtension']=build_extension_view({'sources':{g:_empty(g) for g in ('usdcnh','nominalGdp','vix')},'marketSummary':result['full']})
    old=result['full'];new=controlled(lookback=6,at=NOW+timedelta(days=1))
    for key in new['groups']:new['groups'][key]['targets']=old['groups'][key]['targets']+new['groups'][key]['targets']
    result['shortenedWindow']=build_exchange_summary_view({'analysisDate':new['analysisDate'],'groups':new['groups']})
    from app.macro.sz_market_calibration import build_proof
    from app.macro.sz_market_sample import assemble_sample_plan, SzMarketSamplePolicy
    from backend.tests.macro.test_sz_market_sample import policy
    from backend.tests.macro.test_sz_market_calibration import data
    summary=controlled();target=summary['groups']['aShareFloatMv']['targets'][0];receipt=target['documents'][1]
    receipt['businessData']['items']=[];receipt['businessSha256']=digest(receipt['businessData'])
    receipt['normalized']={'source':'tushare','status':'empty','points':[]};receipt['coreBindings']=[]
    plan=assemble_sample_plan(SzMarketSamplePolicy.model_validate(policy(receipt['runId'])),
        {'runId':receipt['runId'],'planFingerprint':target['plan']['fingerprint'],'businessSha256':receipt['businessSha256']},target['plan']['targetDate'])
    raw=data()
    for row in raw['items']:row[0]=plan['targetDate'].replace('-','')
    from app.macro.sz_market_sample_store import RUN
    sample={'runId':RUN,'rawId':123,'plan':plan,'businessData':raw,'businessSha256':digest(raw),
        'retrievedAt':(NOW+timedelta(minutes=1)).isoformat(),'completedAt':(NOW+timedelta(minutes=1)).isoformat()}
    proof=build_proof(sample,(NOW+timedelta(minutes=2)).isoformat(),999999)
    result['calibrated']=build_exchange_summary_view({'analysisDate':summary['analysisDate'],'groups':summary['groups'],'calibration':proof})
    summary['groups']['marginBalances']={'targets':[],'latestAttempt':None}
    result['calibratedNoMargin']=build_exchange_summary_view({'analysisDate':summary['analysisDate'],'groups':summary['groups'],'calibration':proof})
    result['calibratedExtension']=build_extension_view({'sources':{g:_empty(g) for g in ('usdcnh','nominalGdp','vix')},'marketSummary':result['calibrated']})
    return result


if __name__=='__main__':print(json.dumps(cases(),ensure_ascii=True,allow_nan=False))
