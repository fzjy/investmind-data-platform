"""本轮补充纯计算验收；登记在backend.tests经隔离入口，不连接业务库。"""
from dataclasses import replace
import json

from backend.tests.macro.test_enterprise_distribution import distribution_inputs, result


if __name__=='__main__':
    data=distribution_inputs()
    rows=tuple(r for r in data.financials if r.table=='cashflow')
    assert all('c_pay_acq_const_fiolta' not in r.values for r in rows if r.end_date.year==2023)
    value=result(replace(data,financials=rows))
    assert all(not c['points'] for c in value['cards'][:3])
    point=value['cards'][-1]['points'][-1]
    assert (point['primary'],point['secondary'],point['distributionBase'],point['distributionYoY'])==(2,10,4,150)
    print(json.dumps({'status':'passed','onlyCashflowInput':True,'oldCapexAbsent':True,'legacyYoyCardsUnavailableDoNotClearDistribution':True}))
