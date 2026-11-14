"""离线跨语言样例：真实Python构建器及选择证明，绝不连接业务库。"""
from datetime import date
import json
import sys
from app.macro.enterprise import EnterprisePolicy
from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis
from backend.tests.macro.test_renewal_estimation import enabled

parent=public_enterprise_analysis(enabled(),date(2024,8,1),EnterprisePolicy(min_samples=1),rea_enabled=True,industry_enabled=True)
assert valid_enterprise_analysis(parent)
if 'large-hash' in sys.argv:
    from copy import deepcopy
    from app.macro.renewal_estimation import evidence_hash
    q=parent['rea']['series'][4]['quality'][-1]
    members=[{**deepcopy(q['depreciationEvidence']['members'][0]),'symbol':f'{i:06d}.SZ'} for i in range(1500)]
    print(json.dumps({'period':q['period'],'members':members,'hash':evidence_hash(q['period'],members)}))
else:print(json.dumps(parent,allow_nan=False))
