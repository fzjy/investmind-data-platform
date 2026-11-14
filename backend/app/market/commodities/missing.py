"""只协调已验证国内商品的通用占位，不撤销具体故障。"""
from app.market.commodities.dto import valid_commodity_payload


def reconcile_commodity_missing(missing, verified_package):
    if not verified_package or not valid_commodity_payload(verified_package):
        return tuple(missing)
    generic = {'source': 'commodities', 'reason': '尚未采集真实来源'}
    remaining = [row for row in missing if row != generic]
    sources = {row.get('source') for row in remaining}
    for source, reason in (
        ('commodities:overseas', '海外商品真实来源未接入'),
        ('commodities:intraday', '日内商品真实来源未接入'),
    ):
        if source not in sources:
            remaining.append({'source': source, 'reason': reason})
    return tuple(remaining)
