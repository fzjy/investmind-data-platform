"""实际Python v7导出，仅受控输入，不连接用户数据库/供应商。"""
from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
from app.macro.enterprise_contract import valid_enterprise_analysis
from backend.tests.macro.test_enterprise_orders import orders_inputs, result

if __name__=='__main__':
    normal=result();bad=deepcopy(normal);bad['ledger']['metrics'][2]['points'][0]['orderRevenueSpread']=999
    assert not valid_enterprise_analysis(bad)
    data=orders_inputs();negative=replace(data,financials=tuple(replace(r,values={**r.values,'revenue':-1})
        if r.table=='income' and r.end_date.year==2023 else r for r in data.financials))
    near=orders_inputs(current=(1000000000,0),old=(900000000,0))
    near=replace(near,financials=tuple(replace(r,values={**r.values,'revenue':(1000000001 if r.end_date.year==2024 else 900000001) if r.symbol==near.symbols[0] else 0}) if r.table=='income' else r for r in near.financials))
    print(json.dumps({'normal':normal,'zero':result(orders_inputs(old=(0,0))),
        'negativeRevenue':result(negative),'invalid':bad,'gap':result(data,date(2024,8,1)),
        'underflow':result(orders_inputs(current=('1e-10000',0))),'near':result(near)},allow_nan=False))
