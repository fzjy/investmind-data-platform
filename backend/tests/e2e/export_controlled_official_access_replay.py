"""纯合成恢复证明；不读取真实配置、不连接数据库、不发起HTTP。"""
from copy import deepcopy
from datetime import timedelta
import json
from uuid import UUID, uuid5

from app.macro.official_macro_store import normalize_document, run_key
from app.macro.official_macro_view import build_official_macro_view
from backend.tests.e2e.export_controlled_official_macro import cases as existing_cases
from backend.tests.macro.test_household_discovery import plan, directory, REPORT
from backend.tests.macro.test_official_household_credit import document
from backend.tests.macro.test_official_macro_plan import NOW


def raw_groups(view):
    return {key:{field:deepcopy(group[field]) for field in ('plan','documents','latestAttempt')}
            for key,group in view['groups'].items()}


def cases():
    legacy = existing_cases()['full']
    groups = raw_groups(legacy)
    group = groups['householdCredit']
    doc = group['documents'][0]
    parent = doc['runId']
    doc['runId'] = str(uuid5(UUID('8df15db1-3268-408d-82cd-aab9f8917dd9'), parent))
    doc['retrievedAt'] = (NOW+timedelta(microseconds=200)).isoformat()
    doc['recovery'] = dict(parentRunId=parent,
        accessCheckRunId=str(uuid5(UUID('e996cd69-5039-44c1-88f8-caf902a0268a'), parent)),
        parentEvidenceHash='a'*64, accessCheckEvidenceHash='b'*64,
        parentFailedAt=(NOW+timedelta(microseconds=100)).isoformat(),
        accessCheckCompletedAt=(NOW+timedelta(microseconds=300)).isoformat(),
        additionalHttpCount=1, replayHttpCount=0)
    full = build_official_macro_view(dict(analysisDate=legacy['analysisDate'], groups=groups))

    # 新周期目录继续引用恢复后的静态旧根；全部日期与ID由合成计划重算。
    groups = raw_groups(legacy)
    now = NOW.replace(month=11,day=2)
    p = plan(now=now)
    current = dict(plan=p,documents=[])
    first,second = p['documents']
    request = dict(url=first['url'],period=first['period'],directoryRunId=None,directoryRawSha256=None)
    normalized = normalize_document(p,first,request,directory([(REPORT,'2026年前十个月金融统计数据报告')]))
    current['documents'].append(dict(runId=run_key(p,first),documentId=first['id'],request=request,
        retrievedAt=now.isoformat(),completedAt=(now+timedelta(seconds=1)).isoformat(),normalized=normalized))
    request = dict(url=REPORT,period='2026-10',directoryRunId=current['documents'][0]['runId'],
        directoryRawSha256=normalized['rawSha256'])
    current['documents'].append(dict(runId=run_key(p,second),documentId=second['id'],request=request,
        retrievedAt=(now+timedelta(seconds=2)).isoformat(),completedAt=(now+timedelta(seconds=3)).isoformat(),
        normalized=normalize_document(p,second,request,document(title='2026年前十个月金融统计数据报告',amount='1.40'))))
    current['latestAttempt'] = dict(plan=p,status='completed',errorCode=None,
        startedAt=now.isoformat(),finishedAt=(now+timedelta(seconds=3)).isoformat())
    current['history'] = [deepcopy(full['groups']['householdCredit'])]
    groups['householdCredit'] = current
    with_history = build_official_macro_view(dict(analysisDate='2026-11-02',groups=groups))
    return dict(full=full, withHistory=with_history, legacy=legacy)


if __name__ == '__main__':
    print(json.dumps(cases(),ensure_ascii=True,allow_nan=False))
