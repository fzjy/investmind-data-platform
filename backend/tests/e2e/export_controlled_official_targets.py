"""跨语言实际序列化；受控非50成员，仅用离线配置和金额。"""
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from app.macro.enterprise import EnterprisePolicy,MarketValueInput,TradingDayInput
from app.macro.enterprise_contract import public_enterprise_analysis,valid_enterprise_analysis
from app.macro.enterprise_official_targets import build_official_target_comparison
from backend.tests.macro.test_enterprise_industries import inputs
from backend.tests.macro.test_enterprise_target_catalog import payload,load
from backend.tests.macro.test_official_member_policy import policy
from backend.tests.macro.test_enterprise_official_targets import witness

data=inputs(12)
data=replace(data,financials=tuple(replace(r,values={**r.values,**(
    {'n_income_attr_p':r.values['n_income']} if r.table=='income'
    else {'inventories':10,'accounts_receiv':10} if r.table=='balance' else {})}) for r in data.financials),
    industries=tuple(replace(r,root_code='801780.SI') if r.symbol in data.symbols[-3:] else r for r in data.industries),
    cashflow_enabled=True,distribution_enabled=True,ledger_enabled=True,orders_enabled=True,loss_enabled=True,
    trading_days=(TradingDayInput(date(2024,9,2),True,'calendar:1'),),
    market_values=tuple(MarketValueInput(s,date(2024,9,2),100,'mv:'+s) for s in data.symbols))
as_of=date(2026,10,25);p=EnterprisePolicy(min_samples=5)
result=public_enterprise_analysis(data,as_of,p,rea_enabled=True,industry_enabled=True)
with TemporaryDirectory() as directory:
    config=payload();config['officialEvidence']=policy();catalog=load(Path(directory),config)
    proofs=[witness([*data.symbols[:8],'999999.SH']),witness(list(data.symbols[-3:]),'512880.SH','etf')]
    for proof in proofs:proof['catalogFingerprint']=catalog.fingerprint
    result['officialTargets']=build_official_target_comparison(data,as_of,p,result,catalog,proofs)
assert valid_enterprise_analysis(result)
print(json.dumps(result,allow_nan=False))
