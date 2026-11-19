"""只读已入库的明确持股比例和日估值，不推测未知单位或公告时点。"""
from collections import defaultdict
from datetime import date
from math import isfinite,sqrt
from sqlalchemy import select
from app.db.models import TopHolder,DailyBasic
from app.shared.stock_mapping import StockMappingPolicy
from app.stocks.dataset_query import daily_rows_query

def number(v):
    if v is None or isinstance(v,bool):return None
    try:
        n=float(v)
        return n if isfinite(n) else None
    except (TypeError,ValueError,OverflowError):return None

def load_holders(factory,symbols,as_of):
    result=defaultdict(list)
    basics=defaultdict(list)
    if factory is not None:
        with factory() as session:
            for row in session.scalars(select(TopHolder).where(TopHolder.ts_code.in_(symbols),TopHolder.end_date<=as_of,TopHolder.ann_date.is_not(None),TopHolder.ann_date<=as_of)):
                result[row.ts_code].append(row)
            for row in session.scalars(daily_rows_query(DailyBasic, date.min, as_of, symbols)):
                basics[row.ts_code].append(row)
    return {code:holder_periods(rows,basics=basics[code]) for code,rows in result.items()}

def holder_periods(rows,*,basics=()):
    policy=StockMappingPolicy.load()
    groups=defaultdict(list)
    for r in rows:groups[r.end_date].append(r)
    output=[]
    for period,items in sorted(groups.items()):
        eligible=[b for b in basics if b.trade_date<=period and (period-b.trade_date).days<=policy.institution.maxDenominatorLagDays]
        day=max((b.trade_date for b in eligible),default=None)
        caps={number((b.data or {}).get('total_share')) for b in eligible if b.trade_date==day}
        capital=next(iter(caps))*10000 if len(caps)==1 and None not in caps and next(iter(caps))>0 else None
        newest=max(r.ann_date for r in items)
        items=[r for r in items if r.ann_date==newest]
        names=defaultdict(list);invalid=False;holders=[]
        for row in items:
            name=(row.holder_name or '').strip()
            if not name:invalid=True
            names[name].append(row)
        for name,versions in names.items():
            latest=max(r.ann_date for r in versions)
            same=[r for r in versions if r.ann_date==latest]
            def ratio(r):
                explicit=number((r.data or {}).get('hold_ratio'))
                if (r.data or {}).get('hold_ratio') not in (None,''):return explicit
                amount=number(getattr(r,'hold_amount',None))
                return amount/capital*100 if amount is not None and amount>=0 and capital else None
            ratios={ratio(r) for r in same}
            if len(ratios)!=1 or None in ratios:invalid=True;continue
            ratio=next(iter(ratios))
            if not 0<=ratio<=100:invalid=True;continue
            types={getattr(r,'holder_type',None) for r in same}
            if len(types)>1:invalid=True;continue
            prior=next((h for h in output[-1]['holders'] if h['name']==name),None) if output else None
            change=f"较上期{ratio-prior['ratio']:+.2f}pp" if prior else '新进入前十（非首次买入）' if output and output[-1]['holders'] else '无可比前期'
            holders.append({'name':name,'ratio':ratio,'cumulative':0,'change':change,'color':'#0ea5e9',
                'basis':'来源总股本比例' if all(number((r.data or {}).get('hold_ratio')) is not None for r in same) else '报告期持股数量/总股本',
                'holderType':next(iter(types)),'institutionKind':policy.holder_kind(next(iter(types)))})
        holders.sort(key=lambda r:(-r['ratio'],r['name']))
        total=0
        palette=['#0ea5e9','#10b981','#f59e0b','#a855f7','#f43f5e','#14b8a6','#6366f1','#ec4899','#84cc16','#f97316']
        for index,h in enumerate(holders):
            total+=h['ratio'];h['cumulative']=total;h['color']=palette[index%len(palette)]
        if total>100.000001:invalid=True
        institution=sum(h['ratio'] for h in holders if h['institutionKind']=='included')
        output.append({'reportDate':period.isoformat(),'announcementDate':newest.isoformat(),'holders':[] if invalid else holders,
            'institutionHolding':None if invalid or not holders or (any(h['institutionKind']=='unknown' for h in holders) and not any(h['institutionKind']=='included' for h in holders)) else institution,
            'unknownTypeCount':sum(h['institutionKind']=='unknown' for h in holders),
            'reason':'持仓比例缺失、版本冲突或合计异常' if invalid else '总股本口径；仅前十已识别机构覆盖，未知类型未归类，不代表全部机构持仓'})
    return output

def valuation_bands(rows):
    result=[]
    for key in ('pe','pb','ps'):
        points=[]
        for row in sorted(rows,key=lambda r:r.trade_date):
            raw=getattr(row,key,None) if key!='ps' else (row.data or {}).get('ps_ttm',(row.data or {}).get('ps'))
            value=number(raw)
            if value is not None and value>0:points.append({'period':row.trade_date.isoformat(),'value':value})
        mean=sum(p['value'] for p in points)/len(points) if points else 0
        sd=sqrt(sum((p['value']-mean)**2 for p in points)/len(points)) if points else 0
        result.append({'id':key,'label':key.upper(),'current':points[-1]['value'] if points else None,'points':points,'bands':[mean+k*sd for k in (2,1,0,-1,-2)] if points else []})
    return result
