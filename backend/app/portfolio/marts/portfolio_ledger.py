"""组合共用Builder的事件账本适配与DTO门禁。"""
from datetime import date, datetime, timezone
from math import isfinite

from sqlalchemy import select, text

from app.db.models import JobCursor, PublishedSnapshot


def missing_ledger(symbols, as_of, price_starts, *, source_status='not_captured', reason='尚无已验证分红原件'):
    return {'asOf':as_of.isoformat(), 'sourceBaseVersion':'', 'captureIdentity':'',
        'sourceStatus':source_status, 'basis':'observed-supplier-events', 'historyComplete':False,
        'assets':{symbol:{'status':'missing', 'reason':reason, 'coverageStart':price_starts[symbol],
            'coverageEnd':as_of.isoformat(), 'events':[], 'issues':[], 'gaps':[]} for symbol in symbols}}


def load_portfolio_dividend_ledger(factory, symbols, as_of, price_starts):
    with factory(autoflush=False) as session:
        if factory.kw['bind'].dialect.name == 'postgresql':
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        base = session.get(PublishedSnapshot, 'screener')
        if base is None:
            return missing_ledger(symbols, as_of, price_starts)
        day = (base.as_of.replace(tzinfo=timezone.utc) if base.as_of.tzinfo is None else base.as_of).astimezone(timezone.utc).date()
        scope = {row.get('symbol') for row in base.payload.get('stocks', [])}
        if day != as_of or scope != set(symbols):
            return missing_ledger(symbols, as_of, price_starts, source_status='base_changed', reason='当前价格窗口与事件源范围/基准不一致')
        from app.portfolio.evidence import routine_ledger_evidence
        try:
            evidence = routine_ledger_evidence(session,base,symbols,as_of)
        except ValueError as exc:
            if str(exc) in {'DIVIDEND_CORE_CHANGED','DIVIDEND_MANIFEST_BASE_CHANGED'}:
                return missing_ledger(symbols,as_of,price_starts,source_status='base_changed',reason='分红证据与当前股票基版/Core修订尚未对齐，保留旧发布')
            return missing_ledger(symbols,as_of,price_starts,source_status='invalid_evidence',reason='日常分红manifest或原件无效，保留旧发布')
        except (KeyError,TypeError,AttributeError):
            return missing_ledger(symbols,as_of,price_starts,source_status='invalid_evidence',reason='日常分红manifest或原件无效，保留旧发布')
        if evidence is not None:
            from app.portfolio.dividend_ledger import build_dividend_ledger
            return build_dividend_ledger(session,base,symbols,as_of,price_starts,evidence=evidence)
        control = session.scalar(select(JobCursor).where(JobCursor.job_name=='portfolio-dividend-capture', JobCursor.cursor_key=='control'))
        if control is None:
            return missing_ledger(symbols, as_of, price_starts)
        from app.portfolio.dividend_ledger import build_dividend_ledger
        return build_dividend_ledger(session, base, symbols, as_of, price_starts)


def validate_dividend_ledger(model, symbols, as_of):
    errors = []
    def iso(value):
        try:
            return isinstance(value, str) and len(value) == 10 and date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False
    def number(value):
        try:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= 0
        except OverflowError:
            return False
    def original(source):
        if not isinstance(source, dict) or not isinstance(source.get('sourceHash'), str) or not source['sourceHash']:
            return False
        try:
            return datetime.fromisoformat(source['capturedAt']).utcoffset() is not None
        except (ValueError, TypeError, KeyError):
            return False
    if not isinstance(model, dict):
        return ('dividendLedger',)
    if model.get('basis') != 'observed-supplier-events' or model.get('historyComplete') is not False or model.get('asOf') != as_of.isoformat():
        errors.append('dividendLedger.basisOrDate')
    if model.get('sourceStatus') not in {'observed','not_captured','base_changed','invalid_evidence'}:
        errors.append('dividendLedger.sourceStatus')
    for field in ('sourceBaseVersion','captureIdentity'):
        if not isinstance(model.get(field), str) or model.get('sourceStatus') == 'observed' and not model[field]:
            errors.append(f'dividendLedger.{field}')
    assets = model.get('assets')
    if not isinstance(assets, dict) or set(assets) != set(symbols):
        return (*errors, 'dividendLedger.scope')
    for symbol, asset in assets.items():
        path = f'dividendLedger.assets.{symbol}'
        if not isinstance(asset, dict):
            errors.append(path);continue
        if asset.get('status') not in {'observed','missing'} or not isinstance(asset.get('reason'), str):
            errors.append(path+'.status')
        start,end = asset.get('coverageStart'),asset.get('coverageEnd')
        if not iso(start) or not iso(end) or start > end or end > as_of.isoformat():
            errors.append(path+'.coverage')
        events = asset.get('events')
        if not isinstance(events, list):
            errors.append(path+'.events');continue
        ids = set()
        for event in events:
            if not isinstance(event, dict):
                errors.append(path+'.event');continue
            if not isinstance(event.get('id'),str) or not event['id'] or event['id'] in ids:
                errors.append(path+'.event.id')
            ids.add(str(event.get('id')))
            for field in ('periodEnd','announcedAt'):
                if not iso(event.get(field)):
                    errors.append(path+'.event.'+field)
            if iso(event.get('announcedAt')) and event['announcedAt'] > as_of.isoformat():
                errors.append(path+'.event.announcedAt')
            for field in ('implementationDate','recordDate','exDate','payDate','stockListedDate'):
                if event.get(field) is not None and (not iso(event[field]) or event[field] > as_of.isoformat()):
                    errors.append(path+'.event.'+field)
            for field in ('cashPerShare','stockRatio'):
                if event.get(field) is not None and not number(event[field]):
                    errors.append(path+'.event.'+field)
            if event.get('cashStatus') not in {'paid','missing','pending','not_required'} or event.get('capitalStatus') not in {'reported-total','missing','conflict'}:
                errors.append(path+'.event.status')
            if event.get('stockStatus') is not None and event.get('stockStatus') not in {'credited','pending','missing','not_required'}:
                errors.append(path+'.event.stockStatus')
            if event.get('cashStatus') == 'paid' and (event.get('payDate') is None or not number(event.get('cashPerShare'))):
                errors.append(path+'.event.cash')
            if event.get('capitalStatus') == 'reported-total' and not number(event.get('stockRatio')):
                errors.append(path+'.event.capital')
            for field in ('sourceHash','capturedAt','reason'):
                if not isinstance(event.get(field),str) or not event[field] and field != 'reason':
                    errors.append(path+'.event.'+field)
            refs = event.get('sourceIds')
            if not isinstance(refs,list) or not refs or any(not isinstance(x,str) or not x for x in refs):
                errors.append(path+'.event.sourceIds')
            if 'sourceOriginals' in event:
                originals = event['sourceOriginals']
                if (not isinstance(originals, list) or not originals or not isinstance(refs, list)
                    or len(originals) != len(refs)
                    or any(not original(source) or source.get('sourceId') != ref for source, ref in zip(originals, refs))
                    or originals[0].get('sourceHash') != event.get('sourceHash')
                    or originals[0].get('capturedAt') != event.get('capturedAt')):
                    errors.append(path+'.event.sourceOriginals')
        if not isinstance(asset.get('issues'),list) or any(not isinstance(x,str) for x in asset.get('issues',[])):
            errors.append(path+'.issues')
        gaps = asset.get('gaps',[])
        if not isinstance(gaps,list):
            errors.append(path+'.gaps');gaps=[]
        for gap in gaps:
            if not isinstance(gap,dict) or gap.get('affects') not in {'capital','cash','both'} or not isinstance(gap.get('reason'),str) or gap.get('date') is not None and not iso(gap['date']):
                errors.append(path+'.gaps')
        if 'historyVersions' in asset:
            versions = asset['historyVersions']
            if not isinstance(versions, list) or not versions:
                errors.append(path+'.historyVersions')
                continue
            previous = None
            for version in versions:
                if not isinstance(version, dict):
                    errors.append(path+'.historyVersions');continue
                known = version.get('knownAt')
                if (not iso(known) or known < start or known > as_of.isoformat()
                    or previous is not None and known <= previous
                    or previous is None and known != start):
                    errors.append(path+'.historyVersions.knownAt')
                    continue
                previous = known
                nested_asset = {k:v for k,v in asset.items() if k != 'historyVersions'}
                nested_asset.update({key:version.get(key) for key in ('events','gaps','issues')})
                nested = dict(model, assets={symbol:nested_asset})
                for error in validate_dividend_ledger(nested, (symbol,), as_of):
                    errors.append(path+'.historyVersions.'+error)
                for event in version.get('events', []) if isinstance(version.get('events'), list) else []:
                    if isinstance(event, dict) and any(isinstance(event.get(key), str) and event[key] > known
                        for key in ('announcedAt','implementationDate')):
                        errors.append(path+'.historyVersions.publication')
    return tuple(errors)
