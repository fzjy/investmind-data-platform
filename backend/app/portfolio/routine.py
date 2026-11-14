"""统一日常入口的独立组合组；映射与逐代码分红共享持久请求预算。"""
from app.portfolio.evidence import JOB, _get, _save, _seal, _verify, _signatures, _valid_unit, validate_manifest, _query_units, _query_end
from copy import deepcopy
from datetime import datetime, timedelta, timezone, date
import json
from pathlib import Path

from sqlalchemy import text

from app.db.models import PublishedSnapshot, SourcePayload
from app.ingestion.repositories import content_hash
from app.jobs.locks import job_lock_for_engine
from app.portfolio.dividend_capture import _base, _cursor, capture_dividend_evidence, capture_config

CONFIG_FILE = Path(__file__).resolve().parents[3] / 'config' / 'portfolio.json'


def portfolio_routine_config():
    value = json.loads(CONFIG_FILE.read_text(encoding='utf-8')).get('routine', {}) if CONFIG_FILE.is_file() else {}
    defaults = dict(enabled=False, referenceEnabled=True, dividendsEnabled=True,
                    referenceIntervalDays=1, dividendRevisionDays=7)
    result = defaults | value
    for key in ('enabled', 'referenceEnabled', 'dividendsEnabled'):
        if type(result[key]) is not bool:
            raise ValueError('invalid portfolio routine switch')
    for key in ('referenceIntervalDays', 'dividendRevisionDays'):
        if type(result[key]) is not int or not 1 <= result[key] <= 365:
            raise ValueError('invalid portfolio routine interval')
    return result


def _seed(session, base, signatures):
    """只有原冻结基版严格相同才能复用，绝不更新旧control或采集时间。"""
    old = _cursor(session, 'control')
    if old is None:
        return {}
    frozen = old.cursor_value
    keys = ('baseVersion','asOf','symbols','apiName','fields')
    if not isinstance(frozen,dict) or frozen.get('identity') != content_hash({k:frozen.get(k) for k in keys}):
        raise ValueError('DIVIDEND_LEGACY_INVALID')
    for symbol in frozen.get('symbols',[]):
        unit = _cursor(session, f"{frozen['identity']}:unit:{symbol}")
        if unit is not None and unit.cursor_value.get('status') != 'completed':
            raise ValueError('DIVIDEND_LEGACY_UNRESOLVED')
    if any(frozen.get(k) != v for k,v in base.items()):
        return {}
    entries = {}
    for symbol in base['symbols']:
        row = _cursor(session, f"{frozen['identity']}:unit:{symbol}")
        if row is None:
            continue
        unit = deepcopy(row.cursor_value)
        _valid_unit(session, unit, symbol)
        raw = session.get(SourcePayload, unit['rawId'])
        checked = raw.captured_at
        checked = checked.replace(tzinfo=timezone.utc) if checked.tzinfo is None else checked.astimezone(timezone.utc)
        entries[symbol] = dict(unit=unit, coreSignature=signatures[symbol], checkedAt=checked.isoformat())
    return entries


def _entry_query_ends(session, entries, symbols):
    queries = _query_units(session, symbols)
    result = {}
    for symbol in symbols:
        if symbol not in entries:
            continue
        unit = entries[symbol]['unit']
        raw = session.get(SourcePayload, unit['rawId'])
        dates = [_query_end(day, saved, raw) for day, saved in queries[symbol]
            if saved.get('status') == 'completed' and saved.get('rawId') == unit['rawId']
            and saved.get('batchId') == unit['batchId']]
        if dates:
            result[symbol] = min(dates)
    return result


def refresh_dividends(factory, client=None, *, now, execute=False, config=None, revision_days=7, completion_clock=None):
    if now.utcoffset() is None or type(revision_days) is not int or revision_days < 1:
        raise ValueError('aware clock and positive interval required')
    config = capture_config(config)
    lock = job_lock_for_engine(factory.kw['bind'])
    key = JOB + ':plan'
    acquired = execute and lock.try_acquire(key)
    report = dict(status='blocked',requestCount=0,pendingCount=0)
    if execute and not acquired:
        return report | {'code':'DIVIDEND_ROUTINE_LOCK_BUSY'}
    try:
        from app.portfolio.security_mapping import refresh_mapping, security_aliases
        with factory() as s:
            base = _base(s)
        mapping_requests = 0
        remaining = config['maxHttpPerRun']
        if any(symbol.endswith('.BJ') for symbol in base['symbols']):
            mapping = refresh_mapping(factory, client, base=base, now=now, execute=execute,
                config=config, revision_days=revision_days, completion_clock=completion_clock)
            mapping_requests = mapping['requestCount']
            report['requestCount'] = mapping_requests
            if mapping['status'] != 'completed':
                return report | dict(status=mapping['status'], pendingCount=max(1, mapping.get('pendingCount', 1)),
                    code=mapping.get('code', 'DIVIDEND_MAPPING_PENDING'), dryRun=not execute)
            remaining -= max(mapping_requests, mapping.get('reservedThisRun', 0))
            if remaining == 0:
                return report | dict(status='pending', pendingCount=len(base['symbols']), code='DIVIDEND_MAPPING_CAPTURED')
        capture_budget = config | dict(maxHttpPerRun=remaining)
        with factory() as s:
            if factory.kw['bind'].dialect.name=='postgresql':
                s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            base = _base(s)
            aliases, mapping_value = security_aliases(s, base['symbols'])
            query_symbols = sorted({code for codes in aliases.values() for code in codes})
            signatures = _signatures(s, query_symbols)
            saved = _get(s,'manifest')
            if saved is not None:
                manifest = deepcopy(saved.cursor_value)
                validate_manifest(s,manifest)
                entries = manifest['entries']
            else:
                entries = _seed(s,base,signatures)
            query_ends = _entry_query_ends(s, entries, query_symbols)
            active = _get(s,'active')
            active = deepcopy(active.cursor_value) if active is not None else None
            if active and active.get('status')=='pending':
                plan_row = _get(s,active['planKey'])
                if plan_row is None:
                    raise ValueError('DIVIDEND_PLAN_MISSING')
                plan = deepcopy(plan_row.cursor_value)
                _verify(plan)
                if plan['scope'] != base['symbols'] or plan['target']['asOf'] > base['asOf']:
                    raise ValueError('DIVIDEND_SCOPE_CHANGED')
            else:
                due = [symbol for symbol in query_symbols if symbol not in entries
                    or query_ends.get(symbol, '') < base['asOf']
                    or entries[symbol]['coreSignature'] != signatures[symbol]
                    or now - datetime.fromisoformat(entries[symbol]['checkedAt']) >= timedelta(days=revision_days)]
                target = base | dict(symbols=due, cycleDate=now.astimezone(timezone.utc).date().isoformat(),
                                    revisionHash=content_hash({v:signatures[v] for v in due}))
                plan = _seal(dict(target=target, scope=base['symbols'], signatures=signatures,
                                  checkedAt=now.isoformat()))
            identity = content_hash(plan['target'])
            due = plan['target']['symbols']
            report.update(pendingCount=len(due),symbolCount=len(base['symbols']),identity=identity,
                          asOf=base['asOf'],maxHttpPerRun=config['maxHttpPerRun'])
        def frozen_reader(session):
            current = _base(session)
            if current['symbols'] != plan['scope'] or current['asOf'] < plan['target']['asOf']:
                raise ValueError('DIVIDEND_SCOPE_CHANGED')
            return plan['target']
        if not execute:
            if due:
                preview = capture_dividend_evidence(factory,execute=False,config=capture_budget,
                    job=JOB,control_key=f'capture:{identity}',base_reader=frozen_reader)
                if preview['status'] != 'planned':
                    return report | dict(code=preview.get('code','DIVIDEND_PENDING'))
                report['pendingCount'] = preview['pendingCount']
            return report | dict(status='planned',dryRun=True)
        if due:
            plan_key = f'plan:{identity}'
            with factory.begin() as s:
                if _base(s) != base:
                    raise ValueError('DIVIDEND_BASE_CHANGED')
                previous = _get(s,plan_key)
                if previous is None:
                    _save(s,plan_key,plan)
                elif previous.cursor_value != plan:
                    raise ValueError('DIVIDEND_PLAN_CHANGED')
                pointer = dict(status='pending',planKey=plan_key)
                if active != pointer:
                    _save(s,'active',pointer)

            result = capture_dividend_evidence(factory,client,execute=True,config=capture_budget,
                job=JOB,control_key=f'capture:{identity}',base_reader=frozen_reader,clock=completion_clock)
            report.update(requestCount=mapping_requests + result['requestCount'],pendingCount=result.get('pendingCount',len(due)))
            if result['status'] != 'completed':
                return report | dict(status=result['status'],code=result.get('code','DIVIDEND_PENDING'))
            with factory() as s:
                for symbol in due:
                    row = _get(s,f'{identity}:unit:{symbol}')
                    if row is None:
                        raise ValueError('DIVIDEND_UNIT_MISSING')
                    unit = deepcopy(row.cursor_value)
                    _valid_unit(s,unit,symbol)
                    raw = s.get(SourcePayload,unit['rawId'])
                    captured = raw.captured_at
                    captured = captured.replace(tzinfo=timezone.utc) if captured.tzinfo is None else captured.astimezone(timezone.utc)
                    entries[symbol] = dict(unit=unit,coreSignature=plan['signatures'][symbol],checkedAt=unit.get('completedAt',captured.isoformat()))
        with factory.begin() as s:
            current = _base(s)
            if current['symbols'] != plan['scope'] or current['asOf'] < base['asOf']:
                raise ValueError('DIVIDEND_BASE_CHANGED')
            manifest_data = current | {'entries':{v:entries[v] for v in query_symbols}}
            if query_symbols != current['symbols']:
                manifest_data.update(querySymbols=query_symbols, aliases=aliases)
            manifest = _seal(manifest_data)
            validate_manifest(s,manifest)
            previous = _get(s,'manifest')
            if previous is None or previous.cursor_value != manifest:
                _save(s,'manifest',manifest)
            if due:
                _save(s,'active',dict(status='completed',identity=identity))
            current_signatures = _signatures(s,query_symbols)
            changed = [v for v in query_symbols if entries[v]['coreSignature'] != current_signatures[v]]
            latest_ends = _entry_query_ends(s, entries, query_symbols)
            expired = [v for v in query_symbols if latest_ends.get(v, '') < current['asOf']]
        if changed:
            return report | dict(status='pending',code='DIVIDEND_CORE_CHANGED',pendingCount=len(changed),publishReady=False)
        if expired:
            return report | dict(status='pending',code='DIVIDEND_QUERY_EXPIRED',pendingCount=len(expired),publishReady=False)
        return report | dict(status='completed',pendingCount=0,publishReady=True)
    except (ValueError,KeyError,TypeError,AttributeError) as exc:
        code = str(exc) if isinstance(exc,ValueError) and str(exc).startswith('DIVIDEND_') else 'DIVIDEND_EVIDENCE_INVALID'
        return report | dict(status='blocked',code=code)
    finally:
        if acquired:
            lock.release(key)


def run_portfolio_step(settings, *, now, dry_run, publish):
    from contextlib import ExitStack
    from app.db.session import create_engine_and_session
    from app.shared.readonly_database import open_readonly_factory
    from app.portfolio.reference_catalog import run_catalog
    from app.portfolio.reference_history import run_history
    from app.portfolio.marts.portfolio_runtime import rebuild_portfolio
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    config = portfolio_routine_config()
    if not config['enabled']:
        return dict(status='not_selected',requestCount=0)
    with ExitStack() as resources:
        rate = settings.tushare_vip_requests_per_minute if settings.tushare_mode=='vip' else settings.tushare_standard_requests_per_minute
        limiter = RequestRateLimiter(rate)
        if dry_run:
            factory = resources.enter_context(open_readonly_factory(settings.database_url))
        else:
            engine,factory = create_engine_and_session(settings.database_url)
            resources.callback(engine.dispose)
        refs = dict(status='not_selected',requestCount=0)
        dividends = dict(status='not_selected',requestCount=0)
        if config['referenceEnabled']:
            with factory() as s:
                base = _base(s)
                old = s.get(PublishedSnapshot,'portfolio')
                ends = [r['coverage']['end'] for r in (old.payload.get('benchmarks',{}) if old else {}).values()]
            wait = ends and base['asOf'] > min(ends) and (date.fromisoformat(base['asOf'])-date.fromisoformat(min(ends))).days < config['referenceIntervalDays']
            if wait:
                refs = dict(status='not_due',requestCount=0)
            else:
                catalog = run_catalog(settings,factory,execute=not dry_run,rate_limiter=limiter)
                refs = dict(status=catalog['status'],requestCount=catalog['requestCount'],catalog=catalog)
                if not catalog['requestCount'] and catalog.get('catalogReady'):
                    history = run_history(settings,factory,execute=not dry_run,rate_limiter=limiter)
                    refs.update(status=history['status'],history=history,requestCount=history['requestCount'])
                    if not dry_run and history['status']=='completed' and not history.get('historyReady'):
                        refs['status']='blocked'
                elif not dry_run and refs['status']=='completed':
                    refs['status']='pending'
        if config['dividendsEnabled']:
            dividends = refresh_dividends(factory,now=now,execute=False,
                                         revision_days=config['dividendRevisionDays'])
            if not dry_run and dividends['status']=='planned':
                client = None
                if dividends['pendingCount']:
                    if settings.tushare_token is None:
                        return dict(status='configuration_error',code='TUSHARE_TOKEN_REQUIRED',requestCount=refs['requestCount'])
                    from app.connectors.tushare.client import TushareClient
                    budget = capture_config()
                    client = resources.enter_context(TushareClient(settings.tushare_token.get_secret_value(),max_retries=0,
                        timeout_seconds=budget['timeoutSeconds'],rate_limiter=limiter))
                dividends = refresh_dividends(factory,client,now=now,execute=True,
                                              revision_days=config['dividendRevisionDays'])
        report = dict(status='planned' if dry_run else 'partial',requestCount=refs['requestCount']+dividends['requestCount'],
                      references=refs,dividends=dividends,publishReady=False)
        if dry_run:
            return report
        children=(refs,dividends)
        if any('LOCK_BUSY' in str(v.get('code','')) or 'LOCK_BUSY' in str(v.get('history',{}).get('code','')) or 'LOCK_BUSY' in str(v.get('catalog',{}).get('code','')) for v in children):
            report['status']='locked'
        elif any(v['status']=='failed' for v in children):
            report['status']='failed'
        ready = all(v['status'] in {'completed','not_selected'} for v in (refs,dividends))
        if ready:
            report.update(status='completed',publishReady=True)
            if publish:
                report['publication'] = rebuild_portfolio(factory,dry_run=False)
                if report['publication']['status'] not in {'completed','no_change'}:
                    report['status']='locked' if 'LOCK_BUSY' in report['publication'].get('code','') else 'partial'
        return report
