"""纯覆盖门禁；不得依赖发布事务或采集入口。"""

def etf_coverage_regressed(previous,next_payload):
    old=previous.get('heatmapData',{}).get('etf',[])
    if not old:
        return False
    new={t.get('id'):t for t in next_payload.get('heatmapData',{}).get('etf',[])}
    for tile in old:
        candidate=new.get(tile['id'])
        if candidate is None:
            return True
        old_day=tile.get('etf',{}).get('tradeDate')
        new_day=candidate.get('etf',{}).get('tradeDate')
        if old_day and (not new_day or new_day<old_day):
            return True
    dates={t.get('etf',{}).get('tradeDate') for t in new.values() if t.get('assetKind')=='etf'}
    return len(dates)!=1
