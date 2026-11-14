"""订单观测的公开金额重算；余额同比，不把代理解释成订单总额。"""
from fractions import Fraction
from math import isfinite


def growth(current,base,invalid_status):
    if base<=0: return None,invalid_status
    exact=(Fraction(str(current))-Fraction(str(base)))/Fraction(str(base))*100
    try: value=float(exact)
    except OverflowError: return None,'non-finite-ratio'
    if not isfinite(value) or (exact!=0 and value==0): return None,'non-finite-ratio'
    return value,'ready'


def order_values(amounts):
    value,status=growth(amounts['contractLiabilities'],amounts['baseContractLiabilities'],'zero-contract-liability-base')
    revenue,reason=growth(amounts['revenue'],amounts['baseRevenue'],'non-positive-aggregate-base-revenue')
    spread=None
    if value is not None and revenue is not None:
        a={key:Fraction(str(v)) for key,v in amounts.items()}
        exact=(a['contractLiabilities']/a['baseContractLiabilities']-a['revenue']/a['baseRevenue'])*100
        try: spread=float(exact)
        except OverflowError: pass
        if spread is not None and (not isfinite(spread) or (exact!=0 and spread==0)): spread=None
    return {'value':value,'status':status,'revenueYoY':revenue,'revenueYoYStatus':reason,'orderRevenueSpread':spread}
