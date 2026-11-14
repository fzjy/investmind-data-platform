from calendar import monthrange
from datetime import date,timedelta
from decimal import Decimal


def aggregate(daily,period,window_start,as_of):
    if period=='daily': return daily
    groups={}
    for row in daily:
        day=date.fromisoformat(row['date'])
        key=day-timedelta(days=day.weekday()) if period=='weekly' else day.replace(day=1)
        groups.setdefault(key,[]).append(row)
    result=[]
    for start,rows in sorted(groups.items()):
        end=start+timedelta(days=6) if period=='weekly' else start.replace(day=monthrange(start.year,start.month)[1])
        contracts=list(dict.fromkeys(row['contract'] for row in rows))
        result.append({'date':rows[-1]['date'],'periodStart':start.isoformat(),'periodEnd':end.isoformat(),
            'observedFrom':rows[0]['date'],'observedTo':rows[-1]['date'],
            'open':rows[0]['open'],'high':max(r['high'] for r in rows),'low':min(r['low'] for r in rows),'close':rows[-1]['close'],
            'volume':float(sum((Decimal(str(r['volume'])) for r in rows),Decimal(0))),
            'amount':float(sum((Decimal(str(r['amount'])) for r in rows),Decimal(0))),
            'openInterest':rows[-1]['openInterest'],'contracts':contracts,'rollover':len(contracts)>1,
            'partial':window_start>start or as_of<end,'observedDays':len(rows)})
    return result
