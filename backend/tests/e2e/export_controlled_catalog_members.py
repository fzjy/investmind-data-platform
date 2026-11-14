"""扩展目录跨语言合同，受控17家，不连接任何服务。"""
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.macro.enterprise import EnterprisePolicy
from app.macro.enterprise_contract import public_enterprise_analysis,valid_enterprise_analysis
from app.macro.enterprise_official_targets import build_official_target_comparison
from backend.tests.macro.test_catalog_member_policy import value
from backend.tests.macro.test_enterprise_target_catalog import load
from backend.tests.macro.test_enterprise_rea import controlled
from backend.tests.macro.test_enterprise_official_targets import witness


def cases():
    data=controlled(17);asof=date(2026,10,26);policy=EnterprisePolicy(min_samples=1)
    parent=public_enterprise_analysis(data,asof,policy,rea_enabled=True)
    with TemporaryDirectory() as directory:
        catalog=load(Path(directory),value())
        old=witness(list(data.symbols[:8]));new=witness(list(data.symbols[8:13]),code='000905.SH')
        new['indexCode']='000905';new['documents'][0]['url']=new['documents'][0]['url'].replace('000300','000905')
        for proof in (old,new):proof['catalogFingerprint']=catalog.fingerprint
        parent['officialTargets']=build_official_target_comparison(data,asof,policy,parent,catalog,[old,new])
    assert valid_enterprise_analysis(parent)
    return parent


if __name__=='__main__':print(json.dumps(cases(),allow_nan=False))
