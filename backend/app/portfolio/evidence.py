"""组合分红持久证据合同；供读取和更新共用，不调度采集或发布。"""
from datetime import datetime, date, timezone
import binascii
from sqlalchemy import select
from app.db.models import Dividend, SourcePayload, JobCursor, Security
from app.ingestion.repositories import content_hash
from app.portfolio.dividend_capture import _cursor, _valid_saved
from app.portfolio.dividend_ledger import _original_rows
from app.portfolio.dividend_report import DIVIDEND_FIELDS

JOB = 'portfolio-dividend-routine'



def _get(session, key):
    return _cursor(session, key, job=JOB)



def _save(session, key, value):
    from app.portfolio.dividend_capture import _put
    _put(session, key, value, job=JOB)



def _seal(value):
    return value | {'checksum': content_hash(value)}



def _verify(value):
    if not isinstance(value, dict) or value.get('checksum') != content_hash({k:v for k,v in value.items() if k != 'checksum'}):
        raise ValueError('DIVIDEND_MANIFEST_INVALID')



def _signatures(session, symbols):
    rows = session.scalars(select(Dividend).where(Dividend.ts_code.in_(symbols)).order_by(Dividend.id)).all()
    return {symbol: content_hash([{c.key:getattr(row,c.key) for c in Dividend.__table__.columns}
                                  for row in rows if row.ts_code == symbol]) for symbol in symbols}



def _valid_unit(session, unit, symbol):
    if unit.get('status') != 'completed' or not _valid_saved(session, unit, symbol, list(DIVIDEND_FIELDS)):
        raise ValueError('DIVIDEND_SOURCE_INVALID')
    if unit.get('transportRawId', unit['rawId']) != unit['rawId']:
        raise ValueError('DIVIDEND_SOURCE_INVALID')
    try:
        return _original_rows(session.get(SourcePayload, unit['rawId']), list(DIVIDEND_FIELDS))
    except (binascii.Error, ValueError,KeyError,TypeError,AttributeError,OverflowError):
        raise ValueError('DIVIDEND_SOURCE_INVALID') from None



def validate_manifest(session, value):
    _verify(value)
    if value.get('fields') != list(DIVIDEND_FIELDS) or sorted(value.get('entries',{})) != value.get('querySymbols', value.get('symbols')):
        raise ValueError('DIVIDEND_MANIFEST_INVALID')
    for symbol, entry in value['entries'].items():
        checked = datetime.fromisoformat(entry['checkedAt'])
        if checked.utcoffset() is None:
            raise ValueError('DIVIDEND_MANIFEST_INVALID')
        _valid_unit(session, entry['unit'], symbol)


def _verified_code_rows(session, symbols, *, errors=None):
    """加载已绑定的当前及历史原件；后来响应中省略的撤回或更正仍可按公告日核对。"""
    manifest_row = _get(session, 'manifest')
    legacy_row = _cursor(session, 'control')
    units = {}
    historical = {symbol: {} for symbol in symbols}
    if legacy_row:
        frozen = legacy_row.cursor_value
        keys = ('baseVersion', 'asOf', 'symbols', 'apiName', 'fields')
        if (frozen.get('identity') != content_hash({k: frozen.get(k) for k in keys})
            or frozen.get('apiName') != 'dividend' or frozen.get('fields') != list(DIVIDEND_FIELDS)):
            raise ValueError('DIVIDEND_LEGACY_INVALID')
        for symbol in set(symbols).intersection(frozen['symbols']):
            saved = _cursor(session, f"{frozen['identity']}:unit:{symbol}")
            if saved and saved.cursor_value.get('status') == 'completed':
                units[symbol] = saved.cursor_value
                historical[symbol][saved.cursor_value['rawId']] = saved.cursor_value
    for control in session.scalars(select(JobCursor).where(JobCursor.job_name == JOB,
            JobCursor.cursor_key.startswith('capture:'))):
        frozen = control.cursor_value
        identity = frozen.get('identity')
        if (identity != content_hash({k: v for k, v in frozen.items() if k != 'identity'})
                or control.cursor_key != 'capture:' + identity or frozen.get('apiName') != 'dividend'
                or frozen.get('fields') != list(DIVIDEND_FIELDS)):
            raise ValueError('DIVIDEND_CAPTURE_INVALID')
        keys = {f'{identity}:unit:{s}': s for s in set(symbols).intersection(frozen['symbols'])}
        if keys:
            for saved in session.scalars(select(JobCursor).where(JobCursor.job_name == JOB,
                    JobCursor.cursor_key.in_(keys))):
                unit = saved.cursor_value
                if unit.get('status') == 'completed':
                    historical[keys[saved.cursor_key]][unit['rawId']] = unit
    if manifest_row:
        manifest = manifest_row.cursor_value
        _verify(manifest)
        if (manifest.get('fields') != list(DIVIDEND_FIELDS) or manifest.get('apiName') != 'dividend'
            or sorted(manifest.get('entries', {})) != manifest.get('querySymbols', manifest.get('symbols'))):
            raise ValueError('DIVIDEND_MANIFEST_INVALID')
        for symbol in set(symbols).intersection(manifest['entries']):
            units[symbol] = manifest['entries'][symbol]['unit']
    result = {}
    for symbol, history in historical.items():
        current = units.get(symbol)
        if current:
            history[current['rawId']] = current
        if not history:
            continue
        primary = current['rawId'] if current else None
        rows = []
        try:
            for raw_id, unit in sorted(history.items()):
                verified = _valid_unit(session, unit, symbol)
                raw = session.get(SourcePayload, raw_id)
                stamp = raw.captured_at
                stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
                rows.extend(r | {'_verifiedOriginal': True, '_originalSource': dict(
                    sourceId=r['sourceId'], sourceHash=raw.content_hash, capturedAt=stamp.isoformat())} for r in verified)
        except (ValueError, KeyError, TypeError, AttributeError, binascii.Error):
            if errors is None:
                raise
            errors[symbol] = 'DIVIDEND_SOURCE_INVALID'
            continue
        result[symbol] = primary, rows
    return result


def verified_dividend_rows(session, symbols):
    """原件按实际请求代码校验，计算时归并主体；原始代码和行引用不变。"""
    from app.portfolio.security_mapping import security_aliases
    aliases, _ = security_aliases(session, symbols)
    codes = sorted({code for values in aliases.values() for code in values})
    loaded = _verified_code_rows(session, codes)
    result = {}
    for symbol, values in aliases.items():
        rows = []
        primary = loaded.get(symbol, (None, []))[0]
        for code in values:
            if code in loaded:
                raw_id, originals = loaded[code]
                primary = primary or raw_id
                rows.extend(row | {'ts_code': symbol, '_originalTsCode': row['ts_code']} for row in originals)
        if primary is not None or rows:
            result[symbol] = primary, rows
    return result


def _query_units(session, codes):
    """取得请求身份中的查询日期；成功空响应也有单位，失败永不变空查询。"""
    units = {code: [] for code in codes}
    controls = list(session.scalars(select(JobCursor).where(JobCursor.job_name == JOB,
        JobCursor.cursor_key.startswith('capture:'))))
    old = _cursor(session, 'control')
    if old is not None:
        controls.append(old)
    for control in controls:
        frozen = control.cursor_value
        if not isinstance(frozen, dict) or frozen.get('apiName') != 'dividend' or frozen.get('fields') != list(DIVIDEND_FIELDS):
            raise ValueError('DIVIDEND_CAPTURE_INVALID')
        if frozen.get('identity') != content_hash({k:v for k,v in frozen.items() if k != 'identity'}):
            raise ValueError('DIVIDEND_CAPTURE_INVALID')
        for code in set(codes).intersection(frozen['symbols']):
            saved = _cursor(session, f"{frozen['identity']}:unit:{code}", job=control.job_name)
            if saved is not None:
                units[code].append((frozen['asOf'], saved.cursor_value))
    return units


def _latest_query(queries):
    return max(queries, key=lambda value: (value[0],
        value[1].get('completedAt', value[1].get('startedAt', '')), value[1].get('batchId', '')))


def _query_end(as_of, unit, raw):
    stamp = datetime.fromisoformat(unit['completedAt']) if unit.get('completedAt') else raw.captured_at
    stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
    return min(date.fromisoformat(as_of), stamp.date()).isoformat()


def dividend_query_coverage(session, symbols):
    """逐证券全历史请求覆盖；读取不会采集、写库或从缺响应推导零。"""
    from app.portfolio.security_mapping import security_aliases
    symbols = tuple(symbols)
    result = {symbol: dict(querySource=None, sourceIds=[], coverageStart=None, coverageEnd=None, issues=[]) for symbol in symbols}
    try:
        aliases, mapping = security_aliases(session, symbols)
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
        aliases = {symbol: [symbol] for symbol in symbols if not symbol.endswith('.BJ')}
        mapping = None
        for symbol, item in result.items():
            if symbol.endswith('.BJ'):
                item['issues'].append(str(exc) if isinstance(exc, ValueError) else 'mapping_source_invalid')
    try:
        # 所有已绑定原件均须通过同一个严格校验，不能跳过损坏历史。
        codes = sorted({code for values in aliases.values() for code in values})
        errors = {}
        _verified_code_rows(session, codes, errors=errors)
        units = _query_units(session, codes)
    except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
        code = str(exc) if isinstance(exc, ValueError) else 'DIVIDEND_SOURCE_INVALID'
        for item in result.values():
            item['issues'].append(code)
        return result
    securities = {row.ts_code: row for row in session.scalars(select(Security).where(Security.ts_code.in_(symbols)))}
    for symbol, item in result.items():
        listing = securities.get(symbol)
        if listing is not None and listing.list_date is not None:
            item['coverageStart'] = listing.list_date.isoformat()
        else:
            item['issues'].append('missing_listing_date')
        if symbol not in aliases:
            continue
        invalid = [code for code in aliases[symbol] if code in errors]
        if invalid:
            item['issues'].extend('query_invalid:' + code for code in invalid)
            continue
        ends = []
        for code in aliases[symbol]:
            queries = units[code]
            if not queries:
                item['issues'].append('query_missing:' + code)
                continue
            # 同日期可能有多次修订，按真实完成时间决定最新请求；新失败仍阻断。
            as_of, unit = _latest_query(queries)
            if unit.get('status') != 'completed':
                item['issues'].append('query_' + str(unit.get('status', 'invalid')) + ':' + code)
                continue
            try:
                _valid_unit(session, unit, code)
                raw = session.get(SourcePayload, unit['rawId'])
                ends.append(_query_end(as_of, unit, raw))
                source = f'raw.source_payload:{raw.id}'
                item['sourceIds'].append(source)
                if code == symbol:
                    item['querySource'] = source
            except (ValueError, KeyError, TypeError, AttributeError, binascii.Error):
                item['issues'].append('query_invalid:' + code)
        if symbol.endswith('.BJ') and mapping is not None:
            item['sourceIds'].append(f"raw.source_payload:{mapping['unit']['rawId']}")
        # 映射是按复核周期更新的身份元数据，不是现金查询的截止日。
        if len(ends) == len(aliases[symbol]):
            item['coverageEnd'] = min(ends)
        if item['issues']:
            item['querySource'] = None
    return result



def routine_ledger_evidence(session, base, symbols, as_of):
    row = _get(session,'manifest')
    if row is None:
        return None
    manifest = row.cursor_value
    validate_manifest(session,manifest)
    if manifest['baseVersion'] != base.version or manifest['asOf'] != as_of.isoformat() or manifest['symbols'] != sorted(symbols):
        raise ValueError('DIVIDEND_MANIFEST_BASE_CHANGED')
    signatures = _signatures(session,symbols)
    if any(manifest['entries'][v]['coreSignature'] != signatures[v] for v in symbols):
        raise ValueError('DIVIDEND_CORE_CHANGED')
    control = {k:manifest[k] for k in ('baseVersion','asOf','symbols','apiName','fields')}
    control['identity'] = content_hash(control)
    return dict(control=control,units={v:manifest['entries'][v]['unit'] for v in symbols})
