"""真实Python→TS合同；测试专用、隔离输入，不连接业务库。"""
from copy import deepcopy
import json
from app.macro.enterprise_contract import valid_enterprise_analysis
from backend.tests.macro.test_enterprise_ledger import ledger_inputs, result

if __name__=='__main__':
    normal=result();bad=deepcopy(normal);bad['ledger']['metrics'][1]['points'][0]['value']=99
    assert not valid_enterprise_analysis(bad)
    exact=deepcopy(normal)
    exact['ledger']['metrics'][0]['points'][0].update(amounts={'revenue':0.3,'cost':0.1,'baseRevenue':0.9,'baseCost':0.3},value=0,status='ready')
    legacy=deepcopy(exact);legacy['ledger'].pop('calculationVersion')
    legacy['ledger']['metrics'][0]['points'][0]['value']=-1.4210854715202004e-14
    invalid_residue=deepcopy(exact);invalid_residue['ledger']['metrics'][0]['points'][0]['value']=-1.4210854715202004e-14
    assert valid_enterprise_analysis(exact) and valid_enterprise_analysis(legacy) and not valid_enterprise_analysis(invalid_residue)
    print(json.dumps({'normal':normal,'negative':result(ledger_inputs(profit=(-3,-1))),
        'zero':result(ledger_inputs(profit=(-1,1))),'invalid':bad,
        'overflow':result(ledger_inputs(profit=('1e-300',0))),
        'underflow':result(ledger_inputs(profit=('1e-10000',0))),
        'exact':exact,'legacy':legacy,'invalidResidue':invalid_residue},allow_nan=False))
