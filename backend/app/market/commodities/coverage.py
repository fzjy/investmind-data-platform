"""纯覆盖门禁；不得依赖发布事务或采集入口。"""

def commodity_coverage_regressed(previous,next_payload):
    if 'commodityMetadata' not in previous: return False
    try:
        old_day=previous['commodityMetadata']['asOf']; new_day=next_payload['commodityMetadata']['asOf']
        if new_day<old_day: return True
        old={r['code'] for v in previous['commodityData'].values() for r in v['domestic']}
        new={r['code'] for v in next_payload['commodityData'].values() for r in v['domestic'] if r.get('dataKind')=='futures-ohlc' and r.get('tradeDate')==new_day}
        return len(new)!=10 or not old.issubset(new)
    except (KeyError,TypeError,ValueError): return True
