from datetime import date,datetime
from decimal import Decimal, InvalidOperation
from math import isfinite,isclose

from app.market.commodities.contracts import IDENTITIES


def valid_commodity_payload(payload):
    """独立真实商品DTO；旧Fixture合同不在此冒充真实数据。"""
    try:
        metadata=payload['commodityMetadata']; as_of=date.fromisoformat(metadata['asOf'])
        start=date.fromisoformat(metadata['windowStart'])
        if (type(metadata['instrumentCount']) is not int or metadata['instrumentCount']!=10 or start>as_of
            or metadata['adjustment']!='unadjusted' or metadata['overseasStatus']!='not_connected' or metadata['intradayStatus']!='not_connected'): return False
        expected={code:category for code,_,category,_ in IDENTITIES}; codes=[]
        def num(v,positive=False): return type(v) in (int,float) and isfinite(v) and (v>0 if positive else v>=0)
        for category,group in payload['commodityData'].items():
            if group['overseas']!=[]: return False
            for row in group['domestic']:
                code=row['code']; codes.append(code)
                if (expected.get(code)!=category or row['dataKind']!='futures-ohlc' or row['tradeDate']!=as_of.isoformat()
                    or row['currency']!='CNY' or row['amountUnit']!='wan-yuan' or row['volumeUnit']!='hand'
                    or row['changeBasis'] not in ('close/pre_settle-1','close/previous_series_close-1') or row['adjustment']!='unadjusted'
                    or not all(isinstance(row[k],str) and row[k].strip() for k in ('name','source','quoteUnit','tradeUnit','activeContract'))
                    or not num(row['perUnit'],True) or (row['change'] is not None and (type(row['change']) not in (int,float) or not isfinite(row['change'])))): return False
                for key in ('capturedAtFrom','capturedAtTo'):
                    if not isinstance(row[key],str) or datetime.fromisoformat(row[key]).utcoffset() is None: return False
                if datetime.fromisoformat(row['capturedAtFrom'])>datetime.fromisoformat(row['capturedAtTo']): return False
                if row['bars']['intraday']!=[]: return False
                for period in ('daily','weekly','monthly'):
                    bars=row['bars'][period]
                    if not isinstance(bars,list) or not bars: return False
                    dates=[]
                    for bar in bars:
                        day=date.fromisoformat(bar['date']); dates.append(day)
                        if not start<=day<=as_of or not all(num(bar[k],True) for k in ('open','high','low','close')): return False
                        if not bar['low']<=bar['open']<=bar['high'] or not bar['low']<=bar['close']<=bar['high']: return False
                        if not all(num(bar[k]) for k in ('volume','amount','openInterest')): return False
                        if period=='daily' and (not isinstance(bar['contract'],str) or not num(bar['preSettle'],True)): return False
                        if period!='daily' and (type(bar['partial']) is not bool or type(bar['rollover']) is not bool or not bar['contracts']): return False
                    if dates!=sorted(set(dates)): return False
                if row['bars']['daily'][-1]['date']!=row['tradeDate'] or row['bars']['daily'][-1]['contract']!=row['activeContract']: return False
                last=row['bars']['daily'][-1]
                if not isinstance(row['price'],str): return False
                price=Decimal(row['price'])
                # 保留历史快照原口径校验；新包统一与日线序列1D比较。
                base=last['preSettle'] if row['changeBasis']=='close/pre_settle-1' else (row['bars']['daily'][-2]['close'] if len(row['bars']['daily'])>1 else None)
                expected_change=float((Decimal(str(last['close']))/Decimal(str(base))-1)*100) if base is not None else None
                if (not price.is_finite() or price<=0 or price!=Decimal(str(last['close']))
                    or (row['change'] is not None if expected_change is None else (row['change'] is None or not isclose(row['change'],expected_change,rel_tol=1e-10,abs_tol=1e-8)))): return False
        return len(codes)==10 and set(codes)==set(expected)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,InvalidOperation): return False
