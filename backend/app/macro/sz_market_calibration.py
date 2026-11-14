"""已存深市原件的项目限定标定；不是供应商明示单位或独立全市场见证。"""
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal, localcontext
import re
from hashlib import sha256
from uuid import UUID, uuid5

from sqlalchemy import select, text as sql_text

from app.db.models.core import MacroObservation
from app.db.models.ops import JobCursor, JobRun
from app.db.models.raw import SourcePayload
from app.macro.extensions.ingest import utc
from app.macro.extensions.plan import canonical
from app.macro.extensions.store import instant
from app.macro.sz_market_sample import safe_sample, validate_sample_plan
from app.macro.sz_market_sample_store import JOB as SAMPLE_JOB, RUN as SAMPLE_RUN, SzMarketSampleStore

from app.macro.extensions.plan import digest

VERSION = 'sz-market-official-example-calibration-v1'
SERIES = 'cn.market.a_share_float_mv.sz.calibrated'
NAME = '深市两A板块流通市值（官方样例标定）'
SCOPE = '供应商报告的深市主板A股＋创业板A股'
DOCUMENT = 'https://tushare.pro/document/2?doc_id=268'
CONTRACT = {
    'version': VERSION, 'unitBasis': 'official-example-cross-calibrated',
    'sourceUnit': 'CNY-yuan', 'targetUnit': '亿元', 'divisor': '100000000',
    'supplierUnitExplicit': False, 'independentSameDayWitness': False,
    'assumption': '当前接口沿用现行官方文档同日样例的金额单位；未获得当日独立单位见证',
    'references': ['https://tushare.pro/wctapi/documents/215.md', 'https://tushare.pro/wctapi/documents/268.md'],
    'exampleDate': '2020-03-20', 'displayStepYiYuan': '0.01',
    # 金额交叉标定，不将样例之间不一致的证券数量作为范围等价证明。
    'examples': [
        {'scope': '股票', 'yuan': '18400916927839.89', 'displayYiYuan': '184009.16'},
        {'scope': '中小板', 'yuan': '7650247307737.14', 'displayYiYuan': '76502.47'},
        {'scope': '创业板', 'yuan': '4495524754972.39', 'displayYiYuan': '44955.24'},
        {'scope': '主板A＋B', 'yuan': '6255144865130.36', 'displayYiYuan': '62551.44'},
    ],
}
A_BOARDS = ('主板A股', '创业板A股')
STOCK_PARTS = (*A_BOARDS, '主板B股', '主板CDR', '创业板CDR')
NON_STOCK = {'ABS', 'ETF', 'LOF', '不动产基金', '债券', '债券回购', '债券现券', '基金', '封闭式基金', '期权'}


def text(value):
    if value == 0: return '0'
    value = format(value, 'f')
    return value.rstrip('0').rstrip('.') if '.' in value else value


def amount(value):
    if type(value) is not str or not re.fullmatch(r'\d{1,18}(?:\.\d{1,2})?', value):
        raise ValueError('SZ_CALIBRATION_AMOUNT_INVALID')
    return Decimal(value)


def count(value):
    if type(value) is str and re.fullmatch(r'\d{1,15}', value): value = int(value)
    if type(value) is not int or not 0 <= value <= 9007199254740991:
        raise ValueError('SZ_CALIBRATION_COUNT_INVALID')
    return value


def calibrate(data, target):
    if type(target) is not str or date.fromisoformat(target).isoformat() != target:
        raise ValueError('SZ_CALIBRATION_DATE_INVALID')
    if type(data) is not dict or set(data) != {'fields', 'items'}:
        raise ValueError('SZ_CALIBRATION_RAW_INVALID')
    fields, items = data['fields'], data['items']
    if (type(fields) is not list or len(fields) != 4 or set(fields) != {'trade_date', 'ts_code', 'count', 'float_mv'}
        or type(items) is not list or not 4 <= len(items) <= 64):
        raise ValueError('SZ_CALIBRATION_RAW_INVALID')
    rows = {}
    for item in items:
        if type(item) is not list or len(item) != 4: raise ValueError('SZ_CALIBRATION_RAW_INVALID')
        if any(type(cell) not in (str, int, type(None))
            or type(cell) is int and abs(cell)>9007199254740991
            or type(cell) is str and len(cell)>128 for cell in item):
            raise ValueError('SZ_CALIBRATION_RAW_INVALID')
        row = dict(zip(fields, item, strict=True)); code = row['ts_code']
        if (type(code) is not str or code not in {*STOCK_PARTS, '股票', *NON_STOCK} or code in rows
            or row['trade_date'] != target.replace('-', '')):
            raise ValueError('SZ_CALIBRATION_CATEGORY_INVALID')
        rows[code] = row
    if not all(code in rows for code in (*A_BOARDS, '主板B股', '股票')):
        raise ValueError('SZ_CALIBRATION_COVERAGE_INVALID')
    with localcontext() as context:
        context.prec = 80
        values = {code: (count(row['count']), amount(row['float_mv']))
            for code, row in rows.items() if code in {*STOCK_PARTS, '股票'}}
        parts = [values[code] for code in STOCK_PARTS if code in values]
        if sum(p[0] for p in parts) != values['股票'][0] or sum(p[1] for p in parts) != values['股票'][1]:
            raise ValueError('SZ_CALIBRATION_RECONCILIATION_INVALID')
        n = sum(values[code][0] for code in A_BOARDS)
        if n > 9007199254740991: raise ValueError('SZ_CALIBRATION_COUNT_INVALID')
        total = sum(values[code][1] for code in A_BOARDS)
        return {'seriesId': SERIES, 'name': NAME, 'period': target, 'unit': '亿元', 'frequency': 'daily',
            'value': text(total / Decimal(100000000)), 'amountYuan': text(total), 'count': n,
            'scope': SCOPE, 'unitBasis': CONTRACT['unitBasis'], 'supplierUnitExplicit': False,
            'independentSameDayWitness': False, 'contractHash': digest(CONTRACT)}


def run_id(sample):
    return str(uuid5(UUID('0fa970f7-cf09-48b3-9f9e-6657243e1515'),
        f"{VERSION}:{sample['plan']['targetDate']}:{sample['businessSha256']}"))


def stamp(value):
    if type(value) is not str: raise ValueError('SZ_CALIBRATION_TIME_INVALID')
    at = instant(datetime.fromisoformat(value))
    if at.isoformat() != value or not value.endswith('+00:00'):
        raise ValueError('SZ_CALIBRATION_TIME_INVALID')
    return at


def build_proof(sample, normalized_at, core_id):
    if (type(sample) is not dict or set(sample) != {'runId', 'rawId', 'plan', 'businessData', 'businessSha256',
        'retrievedAt', 'completedAt'} or sample['runId'] != SAMPLE_RUN
        or type(sample['rawId']) is not int or not 0 < sample['rawId'] <= 9007199254740991
        or type(core_id) is not int or not 0 < core_id <= 9007199254740991):
        raise ValueError('SZ_CALIBRATION_PROOF_INVALID')
    validate_sample_plan(sample['plan'])
    raw = safe_sample(sample['businessData'], sample['plan'])
    if canonical(raw) != canonical(sample['businessData']) or digest(raw) != sample['businessSha256']:
        raise ValueError('SZ_CALIBRATION_PROOF_INVALID')
    retrieved, completed, normalized = (stamp(v) for v in (sample['retrievedAt'], sample['completedAt'], normalized_at))
    if not retrieved <= completed <= normalized or sample['plan']['targetDate'] > retrieved.date().isoformat():
        raise ValueError('SZ_CALIBRATION_TIME_INVALID')
    point = calibrate(raw, sample['plan']['targetDate'])
    revision = digest({'runId': run_id(sample), 'sourceHash': sample['businessSha256'], 'point': point})
    binding = {'coreId': core_id, 'seriesId': SERIES, 'period': point['period'], 'value': point['value'], 'revision': revision}
    result = {'schemaVersion': 1, 'algorithmVersion': VERSION, 'sample': deepcopy(sample),
        'contract': deepcopy(CONTRACT), 'point': point, 'normalizedAt': normalized_at,
        'runId': run_id(sample), 'coreBinding': binding}
    return {**result, 'evidenceHash': digest(result)}


def proof_valid(value):
    try:
        return (type(value) is dict and canonical(value) == canonical(build_proof(value['sample'],
            value['normalizedAt'], value['coreBinding']['coreId'])))
    except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError): return False


class CalibrationStore:
    def __init__(self, factory): self.factory = factory

    def _sample(self, session, now):
        root = session.scalars(select(JobCursor).where(JobCursor.job_name == SAMPLE_JOB)).one()
        plan = root.cursor_value['plan']
        state = SzMarketSampleStore(self.factory)._state(session, plan, now)
        if state['status'] != 'captured': raise ValueError('SZ_CALIBRATION_SAMPLE_REQUIRED')
        row = session.get(JobRun, SAMPLE_RUN); raw = session.get(SourcePayload, row.details['rawId'])
        return {'runId': SAMPLE_RUN, 'rawId': raw.id, 'plan': deepcopy(plan),
            'businessData': deepcopy(raw.payload['businessData']), 'businessSha256': raw.payload['businessSha256'],
            'retrievedAt': utc(raw.captured_at).isoformat(), 'completedAt': utc(row.finished_at).isoformat()}

    def _existing(self, session, sample, now):
        key = run_id(sample); row = session.get(JobRun, key)
        cores = session.scalars(select(MacroObservation).where(MacroObservation.series_id == SERIES,
            MacroObservation.observed_at == date.fromisoformat(sample['plan']['targetDate']))).all()
        jobs = session.scalars(select(JobRun).where(JobRun.job_name == VERSION)).all()
        if row is None:
            if cores or jobs: raise ValueError('SZ_CALIBRATION_UNPROVED_EXISTING_DATA')
            return None
        if (len(jobs) != 1 or jobs[0].id != key or row.job_name != VERSION
            or row.business_key != sample['plan']['targetDate'] + ':' + sample['businessSha256']
            or row.status != 'completed' or row.request_count != 0 or row.row_count != 1
            or row.error_message is not None or type(row.details) is not dict or set(row.details) != {'proof'}):
            raise ValueError('SZ_CALIBRATION_RUN_INVALID')
        proof = row.details['proof']
        if (not proof_valid(proof) or canonical(proof['sample']) != canonical(sample)
            or utc(row.started_at) != stamp(proof['normalizedAt']) or utc(row.finished_at) != utc(row.started_at)
            or utc(row.finished_at) > now or len(cores) != 1):
            raise ValueError('SZ_CALIBRATION_PROOF_INVALID')
        core = cores[0]; p = proof['point']; b = proof['coreBinding']
        if (core.id != b['coreId'] or core.source != 'tushare-calibrated' or core.series_id != SERIES
            or core.name != NAME or core.unit != '亿元' or core.region != 'CN' or core.frequency != 'daily'
            or core.seasonal_adjustment != 'NSA' or core.observed_at.isoformat() != p['period']
            or utc(core.released_at) != utc(row.finished_at) or core.valid_until is not None
            or core.revision != b['revision'] or core.value != Decimal(p['value']) or core.source_url != DOCUMENT):
            raise ValueError('SZ_CALIBRATION_CORE_INVALID')
        return deepcopy(proof)

    def replay(self, now, *, dry_run=False, clock=None):
        now = instant(now)
        with self.factory.begin() as session:
            if not dry_run and session.bind.dialect.name == 'postgresql':
                key = int.from_bytes(sha256(VERSION.encode()).digest()[:8], 'big', signed=True)
                session.execute(sql_text('SELECT pg_advisory_xact_lock(:key)'), {'key': key})
            if clock is not None:
                current = instant(clock())
                if current < now: raise ValueError('SZ_CALIBRATION_CLOCK_REGRESSION')
                now = current
            sample = self._sample(session, now); prior = self._existing(session, sample, now)
            point = calibrate(sample['businessData'], sample['plan']['targetDate'])
            base = {'supplierHttp': 0, 'publicationRequested': False, 'runId': run_id(sample),
                'point': point, 'insertedRows': 0, 'readOnly': True}
            if prior is not None: return {**base, 'status': 'completed'}
            if dry_run: return {**base, 'status': 'preview'}
            proof = build_proof(sample, now.isoformat(), 1)
            core = MacroObservation(source='tushare-calibrated', series_id=SERIES, name=NAME, region='CN',
                frequency='daily', unit='亿元', seasonal_adjustment='NSA', observed_at=date.fromisoformat(point['period']),
                released_at=now, revision=proof['coreBinding']['revision'], value=Decimal(point['value']), source_url=DOCUMENT)
            session.add(core); session.flush()
            proof = build_proof(sample, now.isoformat(), core.id)
            session.add(JobRun(id=run_id(sample), job_name=VERSION, business_key=point['period']+':'+sample['businessSha256'],
                status='completed', started_at=now, finished_at=now, request_count=0, row_count=1, details={'proof': proof}))
            return {**base, 'status': 'completed', 'readOnly': False, 'insertedRows': 1}

    def load(self, as_of):
        cutoff = instant(as_of)
        with self.factory() as session:
            rows = session.scalars(select(JobRun).where(JobRun.job_name == VERSION, JobRun.started_at <= cutoff)).all()
            if not rows: return None
            return self._existing(session, self._sample(session, cutoff), cutoff)
