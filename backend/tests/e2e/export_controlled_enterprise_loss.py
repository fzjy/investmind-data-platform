"""供实际TypeScript消费者读取的隔离Python合同，不连接业务库。"""
import json
from copy import deepcopy
from app.macro.enterprise_contract import valid_enterprise_analysis
from backend.tests.macro.test_enterprise_loss import loss_inputs, result


if __name__=='__main__':
    normal=result()
    invalid_full=deepcopy(normal);invalid_full['cards'][-1]['quality'][-1]['lossMembers']=normal['symbols'][:]
    invalid_zero=deepcopy(normal);invalid_zero['cards'][-1]['points'][-1].update(
        totalMarketValue=1e300,lossMarketValue=1e-300,declineMarketValue=1e300,primary=0,secondary=100,result=-100)
    assert not valid_enterprise_analysis(invalid_full) and not valid_enterprise_analysis(invalid_zero)
    invalid_asof=deepcopy(normal);invalid_asof['asOfDate']='20240531'
    invalid_valuation=deepcopy(normal)
    invalid_valuation['cards'][-1]['quality'][-1]['valuationDate']='20240506'
    invalid_valuation['cards'][-1]['points'][-1]['valuationDate']='20240506'
    assert not valid_enterprise_analysis(invalid_asof) and not valid_enterprise_analysis(invalid_valuation)
    print(json.dumps({'normal':normal,'zeroLoss':result(loss_inputs((0,1),(0,0))),
        'overlap':result(loss_inputs((-3,-1),(-2,2))),
        'underflow':result(loss_inputs(weights=('1e-10000',90000))),
        'invalidFullSet':invalid_full,'invalidFalseZero':invalid_zero,
        'invalidCompactAsOf':invalid_asof,'invalidCompactValuation':invalid_valuation},allow_nan=False))
