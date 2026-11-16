from __future__ import annotations

import hashlib
import html
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen, Request

from .storage import Store, StoreLocked

UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8))


class CollectionError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    page_size: int = 20
    max_pages: int = 3
    interval_seconds: int = 300
    request_interval_seconds: float = 1
    timeout_seconds: int = 15
    retry_count: int = 1
    backoff_seconds: int = 2
    overlap_hours: int = 6
    lookback_hours: int = 24
    stale_hours: int = 12
    failure_threshold: int = 2
    retention_days: int = 365
    log_retention_days: int = 90
    cleanup_interval_seconds: int = 86400
    cleanup_batch_size: int = 500
    cleanup_max_batches: int = 20
    worker_retry_seconds: int = 30
    database_connect_timeout_seconds: int = 5
    database_statement_timeout_seconds: int = 30
    database_lock_timeout_seconds: int = 5

    def __post_init__(self):
        for key, maximum in {'page_size': 100, 'max_pages': 20, 'interval_seconds': 86400,
                            'timeout_seconds': 60, 'overlap_hours': 168, 'lookback_hours': 720,
                            'stale_hours': 168, 'failure_threshold': 100, 'retention_days': 36500, 'worker_retry_seconds': 3600,
                            'log_retention_days': 36500, 'cleanup_interval_seconds': 604800,
                            'cleanup_batch_size': 5000, 'cleanup_max_batches': 100,
                            'database_connect_timeout_seconds': 60, 'database_statement_timeout_seconds': 120,
                            'database_lock_timeout_seconds': 30}.items():
            value = getattr(self, key)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f'Invalid {key}')
        if type(self.enabled) is not bool or type(self.retry_count) is not int or not 0 <= self.retry_count <= 3:
            raise ValueError('Invalid enabled/retry_count')
        if not 0 <= self.request_interval_seconds <= 60 or not 0 <= self.backoff_seconds <= 60:
            raise ValueError('Invalid request spacing')

    @classmethod
    def read(cls, path: Path):
        return cls(**json.loads(path.read_text(encoding='utf-8')))


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def normalize(payload, now):
    try:
        result = payload['result']
        if result['status']['code'] != 0:
            raise CollectionError('SOURCE_ERROR')
        rows = result['data']['feed']['list']
        if not isinstance(rows, list):
            raise CollectionError('SCHEMA_CHANGED')
        if not rows:
            raise CollectionError('EMPTY_RESPONSE')
        records = []
        for row in rows:
            identity = int(row['id'])
            if identity <= 0 or row.get('is_delete', 0) not in (0, '0'):
                raise CollectionError('INVALID_RECORD')
            stamp = datetime.strptime(row['create_time'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=SHANGHAI).astimezone(UTC)
            if stamp > now + timedelta(minutes=5):
                raise CollectionError('FUTURE_TIMESTAMP')
            parser = PlainText(); parser.feed(row['rich_text'])
            text = ' '.join(html.unescape(''.join(parser.parts)).split())
            if not text or '\ufffd' in text:
                raise CollectionError('INVALID_TEXT')
            match = re.match(r'^[【〖](.*?)[】〗]', text)
            title = match.group(1) if match else text.split('。')[0]
            # Metadata only; never disguise a full multi-paragraph article as title.
            title = title[:160]
            value = {'schemaVersion': '1.0', 'source': 'sina-finance', 'externalId': str(identity),
                     'title': title, 'summary': None, 'publishedAt': stamp.isoformat(),
                     'sourceUrl': f'https://wap.cj.sina.cn/pc/7x24/{identity}',
                     'securities': [], 'copyrightStatus': 'link_only'}
            try:
                hints = json.loads(row.get('ext') or '{}').get('stocks', [])
                value['securities'] = sorted({h['symbol'][2:] + '.' + h['symbol'][:2].upper()
                    for h in hints if h.get('market') == 'cn' and re.fullmatch(r'(sh|sz|bj)\d{6}', h.get('symbol', ''))})
            except (ValueError, TypeError, AttributeError):
                pass  # 关联提示可缺失，新闻本身不因此伪造证券。
            value['contentHash'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
            value['fetchedAt'] = now.isoformat()
            records.append(value)
        if len({r['externalId'] for r in records}) != len(records):
            raise CollectionError('DUPLICATE_PAGE_ID')
        return records
    except CollectionError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CollectionError('SCHEMA_CHANGED') from exc


def fetch_sina(params, timeout):
    url = 'https://zhibo.sina.com.cn/api/zhibo/feed?' + urlencode(params)
    request = Request(url, headers={'User-Agent': 'SmartParseNewsCollector/0.1', 'Accept': 'application/json'})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise CollectionError('RESPONSE_TOO_LARGE')
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise CollectionError('SCHEMA_CHANGED') from exc


def initial_state():
    return dict(nextId=None, cycleAt=None, cutoff=None, lastSuccessAt=None, lastAttemptAt=None,
                latestPublishedAt=None, failures=0, lastError=None, recoveredAt=None, requestCount=0)


class Collector:
    def __init__(self, config: Config, *, store: Store, fetch=None, sleep=time.sleep, stopped=lambda: False):
        self.config, self.sleep, self.stopped = config, sleep, stopped
        self.fetch = fetch or (lambda params: fetch_sina(params, config.timeout_seconds))
        self.store = store

    def status(self): return self.store.status()
    def pending(self, limit=500): return self.store.pending(limit)
    def ack(self, ids): self.store.ack(ids)

    def run(self, now=None):
        now = (now or datetime.now(UTC)).astimezone(UTC)
        c = self.config
        if not c.enabled:
            return {'status': 'disabled', 'requestCount': 0}
        try:
            with self.store.transaction() as tx:
                return self._run(tx, now)
        except StoreLocked:
            return {'status': 'locked', 'requestCount': 0}

    def _run(self, tx, now):
        c = self.config
        state = tx.load()
        if state['lastAttemptAt'] and (now-datetime.fromisoformat(state['lastAttemptAt'])).total_seconds() < c.interval_seconds:
            return {'status': 'not_due', 'requestCount': 0}
        state['lastAttemptAt'] = now.isoformat()
        if not state['cycleAt']:
            state['cycleAt'] = now.isoformat()
            base = datetime.fromisoformat(state['lastSuccessAt']) if state['lastSuccessAt'] else now
            hours = c.overlap_hours if state['lastSuccessAt'] else c.lookback_hours
            state['cutoff'] = (base-timedelta(hours=hours)).isoformat()
        calls = 0
        status = 'catching_up'
        for _ in range(c.max_pages):
            if self.stopped():
                status = "stopped"
                break
            params = dict(zhibo_id=152, page_size=c.page_size, dire='f', dpc=1)
            if state['nextId']:
                params.update(id=state['nextId'], type=1)
            try:
                for attempt in range(c.retry_count+1):
                    self.sleep(c.request_interval_seconds)
                    if self.stopped():
                        raise CollectionError("STOPPED")
                    calls += 1
                    try:
                        payload = self.fetch(params)
                        break
                    except (TimeoutError, URLError, HTTPError) as exc:
                        retryable = not isinstance(exc, HTTPError) or exc.code == 429 or exc.code >= 500
                        if attempt == c.retry_count or not retryable:
                            raise CollectionError('SOURCE_UNAVAILABLE') from exc
                        self.sleep(c.backoff_seconds * (2**attempt))
                records = normalize(payload, now)
                ids = [int(r['externalId']) for r in records]
                if ids != sorted(ids, reverse=True) or (state['nextId'] and max(ids) >= state['nextId']):
                    raise CollectionError('PAGINATION_STALLED')
                for record in records:
                    if record['publishedAt'] >= state['cutoff']:
                        tx.append(record, now.isoformat())
                newest = max(r['publishedAt'] for r in records)
                state['latestPublishedAt'] = max(state['latestPublishedAt'] or newest, newest)
                state['nextId'] = min(ids)
                if min(r['publishedAt'] for r in records) <= state['cutoff'] or len(records) < c.page_size:
                    state['lastSuccessAt'] = state['cycleAt']
                    state.update(nextId=None, cycleAt=None, cutoff=None)
                    status = 'completed'
                if state['failures']:
                    state['recoveredAt'] = now.isoformat()
                    tx.event(now.isoformat(), 'RECOVERED')
                state.update(failures=0, lastError=None)
                if (now-datetime.fromisoformat(state['latestPublishedAt'])).total_seconds() > c.stale_hours*3600:
                    state['lastError'] = 'SOURCE_STALE'
                if status == 'completed':
                    break
            except CollectionError as exc:
                code = str(exc)
                if code == 'STOPPED':
                    status = 'stopped'
                    break
                if state['lastError'] != code:
                    tx.event(now.isoformat(), code)
                state['failures'] += 1
                state['lastError'] = code
                status = 'failed'
                break
        state['requestCount'] = calls
        tx.save(state)
        tx.expire((now-timedelta(days=c.retention_days)).isoformat())
        return {'status': status, 'requestCount': calls, **state}
