"""独立、有限且可恢复的估值原件供给；不改Core或旧字段注册表。"""
from __future__ import annotations

import base64
from contextlib import ExitStack
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
from types import SimpleNamespace

from sqlalchemy import select, text
from app.connectors.tushare.contracts import request_hash
from app.db.models import IngestionBatch, JobCursor, PublishedSnapshot, SourcePayload
from app.ingestion.repositories import content_hash
from app.jobs.locks import job_lock_for_engine
from app.portfolio.api.dividends import original_dividend_rows
from app.portfolio.api.reference import capture_catalog

CONFIG_FILE = Path(__file__).resolve().parents[3] / 'config' / 'financial-indicators.json'
JOB = 'stock-valuation-evidence'
FIELDS = ('ts_code', 'trade_date', 'pe', 'pb')


def valuation_config():
    value = dict(enabled=False, maxHttpPerRun=20, timeoutSeconds=20, historyYears=5, revisionDays=7)
    if CONFIG_FILE.is_file():
        value.update(json.loads(CONFIG_FILE.read_text(encoding='utf-8')).get('valuationSupply', {}))
    if type(value['enabled']) is not bool:
        raise ValueError('VALUATION_CONFIG_INVALID')
    for key, maximum in [('maxHttpPerRun', 20), ('timeoutSeconds', 120), ('historyYears', 30), ('revisionDays', 365)]:
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError('VALUATION_CONFIG_INVALID')
    return value


def _utc(now):
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _cursor(s, key):
    return s.scalar(select(JobCursor).where(JobCursor.job_name == JOB, JobCursor.cursor_key == key))


def _scope(s):
    p = s.get(PublishedSnapshot, 'screener')
    if p is None:
        raise ValueError('VALUATION_BASE_REQUIRED')
    symbols = sorted(r['symbol'] for r in p.payload['stocks'])
    if not symbols or len(set(symbols)) != len(symbols) or any(not isinstance(v, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', v) for v in symbols):
        raise ValueError('VALUATION_SCOPE_INVALID')
    return dict(scope=symbols, sourceBaseVersion=p.version, asOf=_utc(p.as_of).date().isoformat())


def _start(end, years):
    try:
        return end.replace(year=end.year - years)
    except ValueError:
        return end.replace(year=end.year - years, day=28)


def _contracts(s):
    result = []
    for row in s.scalars(select(JobCursor).where(JobCursor.job_name == JOB, JobCursor.cursor_key.like('contract:%')).order_by(JobCursor.id)):
        c = row.cursor_value
        if row.cursor_key != 'contract:' + content_hash(c) or c.get('fields') != list(FIELDS) or c.get('api') != 'daily_basic':
            raise ValueError('VALUATION_CONTRACT_INVALID')
        scope = c['scope']
        if not scope or scope != sorted(set(scope)) or any(not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', v) for v in scope):
            raise ValueError('VALUATION_CONTRACT_INVALID')
        if c['startDate'] != _start(date.fromisoformat(c['asOf']), c['historyYears']).isoformat() or c['endDate'] != c['asOf'] or not c['sourceBaseVersion']:
            raise ValueError('VALUATION_CONTRACT_INVALID')
        result.append(c)
    return result


def _validate_rows(rows, spec):
    for row in rows:
        if row['ts_code'] != spec['params']['ts_code']:
            raise ValueError('VALUATION_SYMBOL_INVALID')
        day = date.fromisoformat(str(row['trade_date']))
        if not spec['params']['start_date'] <= day.strftime('%Y%m%d') <= spec['params']['end_date']:
            raise ValueError('VALUATION_DATE_INVALID')
        for field in ('pe', 'pb'):
            v = row[field]
            if v is not None and (isinstance(v, bool) or not Decimal(str(v)).is_finite()):
                raise ValueError('VALUATION_NUMBER_INVALID')


def _validate(response, spec):
    _validate_rows(response.rows(), spec)


def _saved(s, contract, spec):
    unit = _cursor(s, f"{content_hash(contract)}:{spec['target']}")
    if unit is None:
        return None
    u = unit.cursor_value
    if u.get('status') != 'completed':
        raise ValueError('VALUATION_UNRESOLVED_RESERVATION')
    raw = s.get(SourcePayload, u.get('rawId'))
    batch = s.get(IngestionBatch, u.get('batchId'))
    digest = request_hash(spec['api'], spec['params'], spec['fields'])
    if raw is None or batch is None or raw.batch_id != batch.id or batch.dataset != JOB or batch.source != 'tushare' or batch.status != 'completed' or batch.request_count != 1 or batch.rejected_count != 0 or batch.finished_at is None or raw.api_name != spec['api'] or raw.request_hash != digest or u.get('requestHash') != digest or raw.content_hash != u.get('responseContentHash') or content_hash(raw.payload) != raw.content_hash:
        raise ValueError('VALUATION_EVIDENCE_INVALID')
    rows = original_dividend_rows(raw, list(spec['fields']), allow_more=True)
    if len(rows) != u.get('rowCount') or len(rows) != batch.row_count:
        raise ValueError('VALUATION_ROW_COUNT_INVALID')
    _validate_rows(rows, spec)
    return raw, rows


def _state(s, c, *, symbols=None):
    specs = [dict(target=symbol, api='daily_basic', fields=list(FIELDS), params=dict(ts_code=symbol, start_date=c['startDate'].replace('-', ''), end_date=c['endDate'].replace('-', ''))) for symbol in c['scope'] if symbols is None or symbol in symbols]
    saved = []; seen = {}; index = 0
    while index < len(specs):
        spec = specs[index]; index += 1
        item = _saved(s, c, spec)
        if item is None:
            continue
        raw, rows = item; saved.append((spec, raw, rows))
        root = spec['params']['ts_code']
        fingerprint = content_hash([{k: r[k] for k in FIELDS} for r in rows])
        prior = seen.setdefault(root, set())
        if rows and fingerprint in prior:
            raise ValueError('VALUATION_PAGING_NO_PROGRESS')
        prior.add(fingerprint)
        data = json.loads(base64.b64decode(raw.payload['_http']['bodyBase64']))['data']
        if data.get('has_more') is True:
            if not rows or len(prior) >= 100:
                raise ValueError('VALUATION_PAGING_LIMIT')
            offset = spec['params'].get('offset', 0) + len(rows)
            specs.append(spec | dict(target=f'{root}:offset:{offset}', params=spec['params'] | dict(offset=offset, limit=8000)))
    return specs, saved, len(saved) == len(specs)


def risk_valuation_history(session, symbols, as_of):
    """风险卡只采用已经完成的批次，以原合同校验每只证券的所有分页。"""
    history = {}
    for contract in sorted(_contracts(session), key=lambda c: (c["asOf"], c["cycle"]), reverse=True):
        codes = set(symbols).intersection(contract["scope"]).difference(history)
        if not codes or contract["asOf"] > as_of.isoformat() or not _cursor(session, "completed:" + content_hash(contract)):
            continue
        _, pages, ready = _state(session, contract, symbols=codes)
        if not ready:
            raise ValueError("VALUATION_EVIDENCE_INVALID")
        for code in codes:
            history[code] = dict(start=contract["startDate"], end=contract["endDate"], rows={})
        for spec, _, rows in pages:
            by_day = history[spec["params"]["ts_code"]]["rows"]
            for row in rows:
                day = date.fromisoformat(str(row["trade_date"])).isoformat()
                pair = [row.get("pe"), row.get("pb")]
                previous = by_day.get(day, pair)
                by_day[day] = [v if v == old else None for v, old in zip(pair, previous)]
    return history


def capture(factory, client=None, *, execute=False, now=None, max_http=None):
    now = _utc(now or datetime.now(timezone.utc))
    spent = 0
    result = dict(status='blocked', requestCount=0)
    lock = job_lock_for_engine(factory.kw['bind'])
    lock_key = JOB + ':targets'  # 固定键；绝不能复用为游标/anchor变量。
    acquired = execute and lock.try_acquire(lock_key)
    if execute and not acquired:
        return result | dict(code='VALUATION_LOCK_BUSY')
    try:
        policy = valuation_config()
        if not policy['enabled']:
            return result | dict(status='not_selected')
        budget = policy['maxHttpPerRun'] if max_http is None else max_http
        if type(budget) is not int or not 1 <= budget <= policy['maxHttpPerRun']:
            raise ValueError('VALUATION_BUDGET_INVALID')
        with factory() as s:
            if factory.kw['bind'].dialect.name == 'postgresql':
                s.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            source = _scope(s)
            if date.fromisoformat(source['asOf']) > now.date():
                raise ValueError('VALUATION_FUTURE_BASE')
            pending = reusable = None
            for old in _contracts(s):
                _, saved, ready = _state(s, old)
                if not ready and pending is None:
                    pending = old
                marker = _cursor(s, 'completed:' + content_hash(old))
                stamp = datetime.fromisoformat(marker.cursor_value['completedAt']) if marker else max((_utc(raw.captured_at) for _, raw, _ in saved), default=now)
                if ready and old['scope'] == source['scope'] and old['asOf'] <= source['asOf'] and old['historyYears'] == policy['historyYears'] and timedelta(0) <= now - _utc(stamp) < timedelta(days=policy['revisionDays']):
                    reusable = old
            c = pending or reusable or (source | dict(api='daily_basic', fields=list(FIELDS), startDate=_start(date.fromisoformat(source['asOf']), policy['historyYears']).isoformat(), endDate=source['asOf'], historyYears=policy['historyYears'], revisionDays=policy['revisionDays'], cycle=now.isoformat()))
            if c['scope'] != source['scope']:
                raise ValueError('VALUATION_FROZEN_SCOPE_CHANGED')
            specs, saved, ready = _state(s, c)
        while execute and not ready and spent < budget:
            report = capture_catalog(factory, client, execute=True, _job=JOB, _requests=specs, _contract=c, _control_key='contract:' + content_hash(c), _config=dict(maxHttpPerRun=budget - spent), _validator=_validate, _stop_on_error=True)
            spent += report['requestCount']
            with factory() as s:
                specs, saved, ready = _state(s, c)
            if report['status'] == 'blocked' or report['failedCount'] or report['requestCount'] == 0:
                raise ValueError('VALUATION_CAPTURE_BLOCKED')
        if execute and ready:
            with factory.begin() as s:
                marker_key = 'completed:' + content_hash(c)
                if _cursor(s, marker_key) is None:
                    s.add(JobCursor(job_name=JOB, cursor_key=marker_key, cursor_value=dict(completedAt=now.isoformat())))
        return dict(status=('completed' if ready else 'pending') if execute else 'planned', requestCount=spent, identity=content_hash(c), unitCount=len(specs), completedCount=len(saved), scope=c['scope'], sourceBaseVersion=c['sourceBaseVersion'], asOf=c['asOf'], compatibleTarget=c['scope'] == source['scope'] and c['asOf'] <= source['asOf'])
    except Exception as exc:
        return result | dict(requestCount=spent, code=str(exc) if isinstance(exc, ValueError) and str(exc).startswith('VALUATION_') else 'VALUATION_EVIDENCE_INVALID')
    finally:
        if acquired:
            lock.release(lock_key)


def load_valuation_rows(factory, symbols, as_of):
    """只读验证原件；目录可为superset，结果保留证据实际截止日期。坏证据抛ValueError。"""
    try:
        with factory() as s:
            contracts = _contracts(s)
            if not contracts or s.get(PublishedSnapshot, 'screener') is None:
                return {}
            source = _scope(s)
            if not set(source['scope']) <= set(symbols):
                return {}
            selected = None
            for c in contracts:
                if c['scope'] != source['scope'] or c['asOf'] > as_of.isoformat():
                    continue
                _, saved, ready = _state(s, c)
                if ready and (selected is None or (c['asOf'], c['cycle']) >= (selected[0]['asOf'], selected[0]['cycle'])):
                    selected = c, saved
            if selected is None:
                return {}
            c, saved = selected
            grouped = {symbol: {} for symbol in c['scope']}
            for spec, _, rows in saved:
                for row in rows:
                    day = date.fromisoformat(str(row['trade_date']))
                    values = grouped[spec['params']['ts_code']].setdefault(day, {'pe': set(), 'pb': set()})
                    for key in ('pe', 'pb'):
                        values[key].add(None if row[key] is None else Decimal(str(row[key])))
            def number(values):
                if len(values) != 1 or None in values:
                    return None
                value = float(next(iter(values)))
                return value if abs(value) != float('inf') else None
            return {symbol: [SimpleNamespace(trade_date=day, pe=number(v['pe']), pb=number(v['pb']), sourceAsOf=date.fromisoformat(c['asOf']), sourceBaseVersion=c['sourceBaseVersion']) for day, v in sorted(days.items())] for symbol, days in grouped.items()}
    except Exception as exc:
        raise ValueError('VALUATION_EVIDENCE_INVALID') from exc


def run_valuation_step(settings, *, now=None, dry_run=True, publish_requested=False):
    from app.db.session import create_engine_and_session
    from app.shared.readonly_database import open_readonly_factory
    from app.connectors.tushare.client import TushareClient
    from app.connectors.tushare.rate_limit import RequestRateLimiter
    policy = valuation_config()
    if not policy['enabled']:
        return dict(status='not_selected', requestCount=0)
    with ExitStack() as resources:
        if dry_run:
            factory = resources.enter_context(open_readonly_factory(settings.database_url))
        else:
            engine, factory = create_engine_and_session(settings.database_url)
            resources.callback(engine.dispose)
        report = capture(factory, now=now)
        from app.stocks.valuation_calendar import capture_calendar
        with factory() as s:current=_scope(s)
        calendar_asof=date.fromisoformat(current['asOf'])
        calendar=capture_calendar(factory,None,calendar_asof,current['sourceBaseVersion'])
        report['calendar']=calendar
        if dry_run or report['status'] != 'planned':
            return report
        client = None
        if report['completedCount'] < report['unitCount'] or calendar['status']!='completed':
            if settings.tushare_token is None:
                return dict(status='blocked', code='TUSHARE_TOKEN_REQUIRED', requestCount=0)
            rate = settings.tushare_vip_requests_per_minute if settings.tushare_mode == 'vip' else settings.tushare_standard_requests_per_minute
            client = resources.enter_context(TushareClient(settings.tushare_token.get_secret_value(), max_retries=0, timeout_seconds=policy['timeoutSeconds'], rate_limiter=RequestRateLimiter(rate)))
        report = capture(factory, client, execute=True, now=now)
        remaining=policy['maxHttpPerRun']-report['requestCount']
        calendar=capture_calendar(factory,client,calendar_asof,current['sourceBaseVersion'],execute=report['status']=='completed',max_http=remaining)
        report['requestCount']+=calendar['requestCount'];report['calendar']=calendar
        if report['status']=='completed' and calendar['status']!='completed':report['status']='pending' if calendar['status'] in ('planned','pending') else 'blocked'
        report['publishReady'] = report['status'] == 'completed' and report['compatibleTarget'] and calendar['publishReady']
        if report['status'] == 'completed' and report['compatibleTarget'] and publish_requested:
            from app.stocks.marts.stock_research_runtime import rebuild_stock_research
            report['publication'] = rebuild_stock_research(factory, dry_run=False)
            if report['publication']['status'] not in ('completed', 'no_change'):
                report['status'] = 'blocked'
        return report
