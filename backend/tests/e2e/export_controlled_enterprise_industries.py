"""Python→TS真实序列化；全部受控输入，不读业务数据库。"""
from dataclasses import replace
from datetime import date
import json
from app.macro.enterprise import MarketValueInput, TradingDayInput, EnterprisePolicy
from app.macro.enterprise_contract import public_enterprise_analysis, valid_enterprise_analysis
from backend.tests.macro.test_enterprise_industries import inputs

data=inputs(12)
data=replace(data,financials=tuple(replace(r,values={**r.values,**(
    {'n_income_attr_p':r.values['n_income']*(2 if r.end_date.year==2024 else 1)} if r.table=='income'
    else {'inventories':10,'accounts_receiv':10} if r.table=='balance' else {})}) for r in data.financials),
    cashflow_enabled=True,distribution_enabled=True,ledger_enabled=True,orders_enabled=True,loss_enabled=True,
    trading_days=(TradingDayInput(date(2024,9,1),False,'calendar:1'),TradingDayInput(date(2024,9,2),True,'calendar:2')),
    market_values=tuple(MarketValueInput(s,date(2024,9,2),100,'mv:'+s) for s in data.symbols))
result=public_enterprise_analysis(data,date(2024,12,1),EnterprisePolicy(min_samples=5),rea_enabled=True,industry_enabled=True)
assert valid_enterprise_analysis(result)
print(json.dumps(result,allow_nan=False))
