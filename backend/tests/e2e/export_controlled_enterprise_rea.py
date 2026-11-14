"""跨语言测试输入；与业务库和供应商隔离。"""
from dataclasses import replace
from datetime import date
import json,sys

from backend.tests.macro.test_enterprise_rea import controlled
from app.macro.enterprise import EnterprisePolicy
from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis

data=controlled()
if 'mixed-period' in sys.argv:
    data=replace(data,financials=tuple(replace(r,values={**r.values,'c_pay_acq_const_fiolta':10 if r.end_date.year==2023 else 70}) if r.table=='cashflow' and r.end_date.month==6 else r for r in data.financials))
if 'underflow' in sys.argv:
    data=replace(data,financials=tuple(replace(r,values={**r.values,'n_cashflow_act':1e-300}) if r.table=='cashflow' and r.end_date.year==2024 else replace(r,values={**r.values,'n_income':1e300}) if r.table=='income' and r.end_date.year==2024 else r for r in data.financials))
data=replace(data,financials=tuple(replace(r,values={**r.values,**({'n_income_attr_p':r.values['n_income']*(2 if r.end_date.year==2024 else 1)} if r.table=='income' else {'inventories':10,'accounts_receiv':10} if r.table=='balance' else {})}) for r in data.financials),
    cashflow_enabled=True,distribution_enabled=True,loss_enabled=True,ledger_enabled=True,orders_enabled=True)
result=public_enterprise_analysis(data,date(2024,8,1),EnterprisePolicy(min_samples=1),rea_enabled=True)
assert valid_enterprise_analysis(result), 'Python parent/projection contract rejected controlled input'
print(json.dumps(result,allow_nan=False))
