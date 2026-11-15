"""官方独立策略不改变原目录身份；拒绝超额与任意源。"""
from copy import deepcopy
from pathlib import Path
import tomllib

import pytest

from backend.tests.macro.test_enterprise_target_catalog import payload, load


def policy():
    def source(key,kind,index,url,**fields):
        return {'id':key,'kind':kind,'indexCode':index,'fundCode':None,
            'subscriptionCode':None,'tradingDate':None,'url':url,**fields}
    base='https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/'
    return {'collectionEnabled':False,'useScope':'local-development-only',
        'bootstrapTargetCodes':['000300.SH','512880.SH'],'maxHttpPerRun':4,'maxHttpPerDay':4,
        'requestsPerMinute':10,'requestTimeoutSeconds':20,'maxRetries':0,
        'maxResponseBytes':8388608,'maxSnapshotMembers':10000,
        'documents':[
            source('csi-000300','csi-members','000300',base+'000300cons.xls'),
            source('csi-399975','csi-members','399975',base+'399975cons.xls'),
            source('fund-512880','fund-mapping','399975','https://e.gtfund.com/etrade/Jijin/view/id/512880#etf-list',fundCode='512880'),
            source('pcf-512880','fund-pcf','399975','https://m.gtfund.com/cochin/etf/download/512881/20261023',
                fundCode='512880',subscriptionCode='512881',tradingDate='2026-10-23'),
        ]}


def test_official_namespace_has_own_fingerprint_without_changing_failed_legacy_plan(tmp_path):
    value=payload(); original=load(tmp_path,value)
    value['officialEvidence']=policy(); current=load(tmp_path,value)
    assert current.fingerprint==original.fingerprint
    assert current.evidence_policy==original.evidence_policy
    assert current.official_policy.collectionEnabled is False
    assert len(current.official_policy.fingerprint)==64
    first=current.official_policy.fingerprint
    value['officialEvidence']['collectionEnabled']=True
    assert load(tmp_path,value).official_policy.fingerprint!=first
    assert load(tmp_path,value).fingerprint==original.fingerprint


@pytest.mark.parametrize('field,bad',[
    ('collectionEnabled',1),('maxHttpPerRun',5),('maxHttpPerDay',5),('maxRetries',1),
    ('requestsPerMinute',11),('requestTimeoutSeconds',21),('maxResponseBytes',8388609),
    ('maxSnapshotMembers',10001),('bootstrapTargetCodes',['000905.SH']),
    ('useScope','production'),('extra',True),
])
def test_official_policy_is_strict_and_bounded(tmp_path,field,bad):
    value=payload(); value['officialEvidence']=policy();value['officialEvidence'][field]=bad
    with pytest.raises(ValueError): load(tmp_path,value)


@pytest.mark.parametrize('mutation',[
    {'url':'https://example.org/000300cons.xls'},
    {'url':'https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/file/autofile/cons/399975cons.xls'},
    {'fundCode':'512880'}, {'indexCode':'000300.SH'}, {'extra':True},
])
def test_closed_official_source_identity(tmp_path,mutation):
    value=payload();value['officialEvidence']=policy()
    value['officialEvidence']['documents'][0].update(mutation)
    with pytest.raises(ValueError):load(tmp_path,value)


def test_duplicate_missing_or_unbound_source_rejected(tmp_path):
    for mutate in [lambda p:p['documents'].append(deepcopy(p['documents'][0])),
                   lambda p:p['documents'].pop(),
                   lambda p:p['documents'][3].update(tradingDate='2026-10-22'),
                   lambda p:p['documents'][2].update(indexCode='000300')]:
        value=payload();value['officialEvidence']=policy();mutate(value['officialEvidence'])
        with pytest.raises(ValueError):load(tmp_path,value)


def test_xls_reader_is_declared_runtime_dependency():
    file=Path(__file__).resolve().parents[3]/'pyproject.toml'
    dependencies=tomllib.loads(file.read_text(encoding='utf-8'))['project']['dependencies']
    assert 'xlrd>=2.0.2,<3' in dependencies


def test_offline_runner_disables_only_new_official_switch_in_an_isolated_copy(tmp_path,monkeypatch):
    import json
    import app.macro.enterprise_target_catalog as catalog
    from backend.tests import run_isolated
    value=payload();value['officialEvidence']=policy();value['officialEvidence']['collectionEnabled']=True
    source=tmp_path/'user-targets.json';source.write_text(json.dumps(value),encoding='utf8')
    monkeypatch.setattr(catalog,'DEFAULT_ENTERPRISE_TARGET_CATALOG_FILE',source)
    original=catalog.load_enterprise_target_catalog();before=source.read_bytes()
    isolated=tmp_path/'isolated';isolated.mkdir()
    run_isolated.isolate_official_member_config(isolated)
    actual=catalog.load_enterprise_target_catalog()
    assert actual.official_policy.collectionEnabled is False and source.read_bytes()==before
    assert actual.fingerprint==original.fingerprint and actual.evidence_policy==original.evidence_policy
