"""供应商当前地域标签分桶；价格日和资料捕获时间是不同口径。"""
import hashlib

from app.market.region.repository import LINEAGE, region_value
from app.shared.stock_mapping import finite_decimal


def known_region_symbols(payload):
    return {stock['symbol'] for sector in payload.get('heatmapData',{}).get('region',[])
        if sector.get('id') != 'region:unknown' for stock in sector.get('stocks',[])}


def region_coverage_regressed(previous, candidate, max_drop_ratio=0.2):
    before, after = known_region_symbols(previous), known_region_symbols(candidate)
    return bool(before and len(before & after) / len(before) < 1 - max_drop_ratio)


def legacy_regions(securities):
    result = {}
    for security in securities:
        data = security.data or {}
        if LINEAGE in data:
            continue  # 没有DB证明的标记，不能降级为legacy。
        try:
            labels = {region_value(data[key]) for key in ('area','region') if data.get(key) not in (None,'')}
            if len(labels) == 1:
                result[security.ts_code] = {'label':labels.pop(),'capturedAt':None}
        except ValueError:
            pass
    return result


def region_groups(prices, basics, names, regions, as_of):
    groups, captured, known, total = {}, [], 0, 0
    by_code = {row.ts_code:row for row in basics}
    for code, price in sorted({row.ts_code:row for row in prices}.items()):
        close = finite_decimal(price.close)
        if close is None or close <= 0:
            continue
        region = regions.get(code)
        label = region['label'] if region else '未知地域'
        key = 'region:'+hashlib.sha256(label.encode()).hexdigest()[:16] if region else 'region:unknown'
        group = groups.setdefault(key,{'id':key,'name':label,'stocks':[]})
        basic = by_code.get(code)
        cap = finite_decimal(basic.total_mv) if basic else None
        amount, change = finite_decimal(price.amount), finite_decimal(price.pct_chg)
        group['stocks'].append({'symbol':code,'name':names.get(code,code),'price':float(close),
            'change':float(change) if change is not None else None,
            'marketCap':float(cap / 10000) if cap is not None else None,
            'turnover':float(amount / 100000) if amount is not None else None})
        total += 1
        if region:
            known += 1
            if region.get('capturedAt'):
                captured.append(region['capturedAt'])
    return groups, {'basis':'当前证券基础资料地域；供应商原标签含城市，非统一省级、非历史注册地',
        'asOf':as_of.isoformat(),'known':known,'unknown':total-known,'total':total,
        'capturedAtFrom':min(captured) if captured else None,'capturedAtTo':max(captured) if captured else None}
