"""供TypeScript实际消费Python生成的受控v3；必须经隔离测试入口。"""
import json

from app.macro.enterprise_contract import public_enterprise_analysis
from backend.tests.macro.test_enterprise_cashflow import AS_OF, POLICY, cash_inputs


def main():
    values={
        'normal':cash_inputs(),
        'subnormal':cash_inputs(cfo=('5e-316',0),capex=('8e-316',0)),
        'cancellation':cash_inputs(cfo=('100000000000000000001',0),capex=('100000000000000000000',0)),
        'ratioOverflow':cash_inputs(cfo=('1e-290',0),capex=('1e300',0)),
    }
    print(json.dumps({k:public_enterprise_analysis(v,AS_OF,POLICY) for k,v in values.items()},allow_nan=False))


if __name__=='__main__': main()
