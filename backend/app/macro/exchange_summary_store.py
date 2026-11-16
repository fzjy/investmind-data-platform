"""具名交易所聚合账本；独立目标日槽位，失败/未知跨日冻结。"""
from datetime import date,datetime
from hashlib import sha256
from uuid import UUID,uuid5

from sqlalchemy import select,text

from app.db.models.core import MacroObservation,TradeCalendar
from app.db.models.ops import JobCursor,JobRun
from app.db.models.raw import IngestionBatch,SourcePayload
from app.macro.exchange_summary_normalize import normalize_exchange_response
from app.macro.exchange_summary_plan import build_exchange_plan,validate_exchange_plan
from app.macro.extensions.ingest import utc
from app.macro.extensions.plan import canonical,digest,scheduled_due
from app.macro.extensions.store import FAILURE_CODES,instant
from app.macro.fiscal_store import core_value

JOB='macro-exchange-summary-v1:'
DATASET='macro-exchange-summary:'
NAMESPACE=UUID('0fa970f7-cf09-48b3-9f9e-6657243e1515')


def run_key(plan,doc):return str(uuid5(NAMESPACE,plan['groupId']+':'+plan['targetDate']+':'+doc['id']))


def plan_from_database(factory,config,group_id,now):
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    policy=config.groups[group_id]
    due=scheduled_due(policy.schedule,now).astimezone(ZoneInfo(policy.publicationTimezone)).date()
    with factory() as session:
        rows=session.scalars(select(TradeCalendar).where(TradeCalendar.exchange.in_(policy.calendarExchanges),
            TradeCalendar.cal_date>=due-timedelta(days=policy.calendarLookbackDays),
            TradeCalendar.cal_date<=due+timedelta(days=policy.calendarLookaheadDays))).all()
        calendar=[{'exchange':r.exchange,'date':r.cal_date.isoformat(),'open':r.is_open,
            'previous':r.pretrade_date.isoformat() if r.pretrade_date else None} for r in rows]
    return build_exchange_plan(group_id,policy.model_dump(),calendar,now)


def safe_business(data,limit):
    # 只允许 JSON 业务列/行；结构错误仍可保存原件，但不能保存任意元数据或对象。
    if type(data) is not dict or set(data)!={'fields','items'}:
        raise ValueError('EXCHANGE_RAW_INVALID')
    fields,items=data['fields'],data['items']
    if (type(fields) is not list or len(fields)>16 or any(type(f) is not str or len(f)>64 for f in fields)
        or type(items) is not list or len(items)>1
        or any(type(row) is not list or len(row)>16 for row in items)
        or any(type(v) not in (str,int,type(None)) or type(v) is str and len(v)>128 for row in items for v in row)
        or len(canonical(data).encode())>limit):
        raise ValueError('EXCHANGE_RAW_INVALID')
    # 金额整数转为文本后再保存业务原件；JS不能无损解码超过2^53的JSON整数。
    # 身份列完全不转换，错误的整数交易日/交易所仍须在规范化阶段拒绝。
    return {'fields':list(fields),'items':[[str(v) if type(v) is int and i<len(fields) and fields[i] in ('rzye','float_mv') else v
        for i,v in enumerate(row)] for row in items]}


class ExchangeSummaryStore:
    def __init__(self,factory):self.factory=factory

    def _root(self,session,plan):
        validate_exchange_plan(plan)
        root=session.scalar(select(JobCursor).where(JobCursor.job_name==JOB+plan['groupId'],JobCursor.cursor_key==plan['targetDate']))
        if root is None or root.cursor_value!={'plan':plan}:raise ValueError('EXCHANGE_ROOT_INVALID')
        return root

    def _raw(self,session,row):
        d=row.details;p=d['plan'];doc=d['document']
        if type(d['rawId']) is not int or d['rawId']<=0:raise ValueError('EXCHANGE_RAW_INVALID')
        raw=session.get(SourcePayload,d['rawId'])
        if (raw is None or raw.batch_id!=row.id or raw.api_name!=doc['query']['api']
            or raw.request_hash!=digest({'plan':p['fingerprint'],'document':doc})):
            raise ValueError('EXCHANGE_RAW_INVALID')
        value=raw.payload
        if (type(value) is not dict or set(value)!={'schemaVersion','planFingerprint','document','businessData','businessSha256','retrievedAt','releaseTimeBasis'}
            or type(value['schemaVersion']) is not int or value['schemaVersion']!=1
            or value['planFingerprint']!=p['fingerprint'] or value['document']!=doc
            or value['releaseTimeBasis']!='normalization-completion-not-source-release'
            or digest(value)!=raw.content_hash):raise ValueError('EXCHANGE_RAW_INVALID')
        data=safe_business(value['businessData'],p['limits']['maxResponseBytes'])
        at=instant(datetime.fromisoformat(value['retrievedAt']))
        if (canonical(data)!=canonical(value['businessData']) or digest(data)!=value['businessSha256'] or at.isoformat()!=value['retrievedAt']
            or at!=utc(raw.captured_at) or at<utc(row.started_at)):raise ValueError('EXCHANGE_RAW_INVALID')
        return data,at

    def _run(self,session,row):
        if row is None:raise ValueError('EXCHANGE_RUN_INVALID')
        d=row.details
        if (type(d) is not dict or set(d)!={'schemaVersion','plan','document','phase','rawId','normalized','coreBindings'}
            or type(d['schemaVersion']) is not int or d['schemaVersion']!=1):raise ValueError('EXCHANGE_RUN_INVALID')
        p,doc,phase=d['plan'],d['document'],d['phase'];self._root(session,p)
        finished=phase in ('completed','failed')
        if (doc not in p['documents'] or row.id!=run_key(p,doc) or row.job_name!=JOB+p['groupId']
            or row.business_key!=p['groupId']+':'+p['targetDate']+':'+doc['id'] or row.request_count!=1
            or phase not in ('reserved','received','completed','failed') or type(d['coreBindings']) is not list
            or row.status!=('running' if not finished else phase) or finished!=(row.finished_at is not None)
            or finished and utc(row.finished_at)<utc(row.started_at)
            or (row.error_message not in FAILURE_CODES if phase=='failed' else row.error_message is not None)
            or phase!='completed' and (d['normalized'] is not None or d['coreBindings'] or row.row_count)
            or phase in ('reserved','failed') and d['rawId'] is not None):raise ValueError('EXCHANGE_RUN_INVALID')
        batch=session.get(IngestionBatch,row.id)
        if (batch is None or batch.source!='tushare' or batch.dataset!=DATASET+p['groupId'] or batch.mode!='live'
            or batch.request_count!=1 or batch.row_count!=row.row_count or batch.rejected_count!=0
            or utc(batch.started_at)!=utc(row.started_at)
            or batch.status!={'reserved':'running','received':'processing','completed':'completed','failed':'failed'}[phase]
            or (batch.finished_at is None)!=(row.finished_at is None)
            or batch.finished_at is not None and utc(batch.finished_at)!=utc(row.finished_at)):
            raise ValueError('EXCHANGE_BATCH_INVALID')
        if phase in ('received','completed'):
            data,retrieved=self._raw(session,row)
            if phase=='completed':
                normalized=normalize_exchange_response(p,doc,data)
                if (retrieved>utc(row.finished_at) or normalized!=d['normalized']
                    or row.row_count!=len(normalized['points']) or len(d['coreBindings'])!=row.row_count):
                    raise ValueError('EXCHANGE_PROOF_INVALID')
                for point,binding in zip(normalized['points'],d['coreBindings'],strict=True):
                    if type(binding) is not dict or set(binding)!={'coreId','seriesId','period','value','revision'} or type(binding['coreId']) is not int:
                        raise ValueError('EXCHANGE_CORE_INVALID')
                    core=session.get(MacroObservation,binding['coreId'])
                    revision=digest({'runId':row.id,'businessSha256':digest(data),'point':point})
                    if (core is None or core.series_id!=point['seriesId'] or core.source!='tushare' or core.region!='CN'
                        or core.frequency!='daily' or core.unit!=point['unit'] or core.name!=point['name'] or core.seasonal_adjustment!='NSA'
                        or core.observed_at.isoformat()!=point['period'] or core.value!=core_value(point['value'])
                        or core.revision!=revision or core.source_url!=p['groupPolicy']['documentationUrl']
                        or utc(core.released_at)!=utc(row.finished_at)
                        or core.valid_until is not None and utc(core.valid_until)<=utc(row.finished_at)
                        or binding!={'coreId':core.id,'seriesId':core.series_id,'period':point['period'],'value':point['value'],'revision':revision}):
                        raise ValueError('EXCHANGE_CORE_INVALID')
        return d

    def inspect(self,plan,now):
        now=instant(now);validate_exchange_plan(plan)
        if instant(datetime.fromisoformat(plan['releaseWindow']['startAt']))>now:raise ValueError('EXCHANGE_CLOCK_REGRESSION')
        with self.factory() as session:
            roots=session.scalars(select(JobCursor).where(JobCursor.job_name==JOB+plan['groupId']).order_by(JobCursor.cursor_key)).all()
            runs=session.scalars(select(JobRun).where(JobRun.job_name==JOB+plan['groupId']).order_by(JobRun.started_at,JobRun.id)).all()
            by_root={}
            for root in roots:
                if type(root.cursor_value) is not dict or set(root.cursor_value)!={'plan'}:raise ValueError('EXCHANGE_ROOT_INVALID')
                old=root.cursor_value['plan'];self._root(session,old)
                if (old['groupId']!=plan['groupId'] or root.cursor_key!=old['targetDate']
                    or old['targetDate']>plan['targetDate'] or utc(root.updated_at)>now):raise ValueError('EXCHANGE_CLOCK_REGRESSION')
                by_root[root.cursor_key]=(old,[])
            for row in runs:
                d=self._run(session,row)
                if utc(row.started_at)>now or row.finished_at is not None and utc(row.finished_at)>now:
                    raise ValueError('EXCHANGE_CLOCK_REGRESSION')
                if d['rawId'] is not None and self._raw(session,row)[1]>now:raise ValueError('EXCHANGE_CLOCK_REGRESSION')
                by_root[d['plan']['targetDate']][1].append(row)
            chosen,selected=plan,[]
            for old,attempts in by_root.values():
                indexes=[old['documents'].index(r.details['document']) for r in attempts]
                if not attempts or sorted(indexes)!=list(range(len(attempts))) or len(attempts)>old['limits']['maxHttpPerPeriod']:
                    raise ValueError('EXCHANGE_ROOT_INVALID')
                if old['targetDate']==plan['targetDate']:
                    if old!=plan:raise ValueError('EXCHANGE_POLICY_CHANGED')
                    chosen,selected=old,attempts
            for old,attempts in by_root.values():
                if len(attempts)<len(old['documents']) or any(r.details['phase']!='completed' for r in attempts):
                    if old['groupPolicyHash']!=plan['groupPolicyHash']:raise ValueError('EXCHANGE_POLICY_CHANGED')
                    chosen,selected=old,attempts;break
            entries=[]
            for doc in chosen['documents']:
                matches=[r for r in selected if r.details['document']==doc]
                row=matches[0] if matches else None
                entries.append({'document':doc,'runId':row.id if row else None,'phase':row.details['phase'] if row else 'new',
                                'errorCode':row.error_message if row else None})
            status=('blocked' if any(e['phase'] in ('reserved','failed') for e in entries) else
                'completed' if all(e['phase']=='completed' for e in entries) else 'partial' if selected else 'new')
            return {'status':status,'plan':chosen,'runId':selected[0].id if selected else None,
                    'reservedCount':len(selected),'documents':entries}

    def reserve(self,plan,doc,now):
        now=instant(now);validate_exchange_plan(plan)
        with self.factory.begin() as session:
            # 所有目标日共享组级事务锁；防止不同 D 并发绕过旧未完成根。
            if session.bind.dialect.name=='postgresql':
                lock=int.from_bytes(sha256((JOB+plan['groupId']).encode()).digest()[:8],'big',signed=True)
                session.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':lock})
            state=self.inspect(plan,now)
            if state['plan']!=plan or state['status']=='blocked' or doc not in plan['documents']:
                raise ValueError('EXCHANGE_RESERVATION_BLOCKED')
            index=plan['documents'].index(doc)
            if (state['documents'][index]['phase']!='new'
                or any(e['phase']!='completed' for e in state['documents'][:index])):
                raise ValueError('EXCHANGE_RESERVATION_BLOCKED')
            if state['runId'] is None:
                from app.macro.exchange_summary_config import validate_exchange_policy
                policy=validate_exchange_policy(plan['groupId'],{**plan['groupPolicy'],'collectionEnabled':False})
                due=scheduled_due(policy.schedule,now)
                window=plan['releaseWindow']
                if not datetime.fromisoformat(window['startAt'])<=due<datetime.fromisoformat(window['nextAt']):
                    raise ValueError('EXCHANGE_UNUSED_TARGET_EXPIRED')
            # 在发送前阻断已有同日事实，不能等消耗请求后才发现无法写入Core。
            new_series=[e['document']['seriesId'] for e in state['documents'] if e['phase']=='new']
            existing_core=session.scalar(select(MacroObservation.id).where(
                MacroObservation.series_id.in_(new_series),MacroObservation.observed_at==date.fromisoformat(plan['targetDate'])).limit(1))
            if existing_core is not None:raise ValueError('EXCHANGE_EXISTING_INPUT_REQUIRES_PROOF')
            if state['runId'] is None:
                session.add(JobCursor(job_name=JOB+plan['groupId'],cursor_key=plan['targetDate'],cursor_value={'plan':plan},updated_at=now))
                session.flush()
            else:self._root(session,plan)
            key=run_key(plan,doc)
            details={'schemaVersion':1,'plan':plan,'document':doc,'phase':'reserved','rawId':None,'normalized':None,'coreBindings':[]}
            session.add(JobRun(id=key,job_name=JOB+plan['groupId'],business_key=plan['groupId']+':'+plan['targetDate']+':'+doc['id'],
                status='running',started_at=now,request_count=1,row_count=0,details=details))
            session.add(IngestionBatch(id=key,source='tushare',dataset=DATASET+plan['groupId'],mode='live',
                status='running',started_at=now,request_count=1,row_count=0,rejected_count=0))
        return key

    def receive(self,key,data,now):
        now=instant(now)
        with self.factory.begin() as session:
            row=session.get(JobRun,key,with_for_update=True);d=self._run(session,row)
            if d['phase']!='reserved' or now<utc(row.started_at):raise ValueError('EXCHANGE_RAW_INVALID')
            data=safe_business(data,d['plan']['limits']['maxResponseBytes'])
            value={'schemaVersion':1,'planFingerprint':d['plan']['fingerprint'],'document':d['document'],
                'businessData':data,'businessSha256':digest(data),'retrievedAt':now.isoformat(),
                'releaseTimeBasis':'normalization-completion-not-source-release'}
            raw=SourcePayload(batch_id=key,api_name=d['document']['query']['api'],
                request_hash=digest({'plan':d['plan']['fingerprint'],'document':d['document']}),
                content_hash=digest(value),payload=value,captured_at=now)
            session.add(raw);session.flush();row.details={**d,'phase':'received','rawId':raw.id}
            session.get(IngestionBatch,key).status='processing'

    def fail(self,key,code,now):
        now=instant(now)
        with self.factory.begin() as session:
            row=session.get(JobRun,key,with_for_update=True);d=self._run(session,row)
            if d['phase']!='reserved' or code not in FAILURE_CODES or now<utc(row.started_at):raise ValueError('EXCHANGE_RUN_INVALID')
            row.details={**d,'phase':'failed'};row.status='failed';row.finished_at=now;row.error_message=code
            batch=session.get(IngestionBatch,key);batch.status='failed';batch.finished_at=now
            from app.acquisition.adapters.plans import record_extension
            record_extension(session,row,now)

    def replay(self,key,now):
        now=instant(now)
        with self.factory.begin() as session:
            row=session.get(JobRun,key,with_for_update=True);d=self._run(session,row)
            if d['phase']=='completed':
                if utc(row.finished_at)>now:raise ValueError('EXCHANGE_CLOCK_REGRESSION')
                return {'status':'completed','supplierHttp':0,'readOnly':True,'runId':key,'insertedRows':0}
            if d['phase']!='received':raise ValueError('EXCHANGE_REPLAY_BLOCKED')
            data,retrieved=self._raw(session,row)
            if now<retrieved:raise ValueError('EXCHANGE_CLOCK_REGRESSION')
            normalized=normalize_exchange_response(d['plan'],d['document'],data);bindings=[]
            for point in normalized['points']:
                previous=session.scalars(select(MacroObservation).where(MacroObservation.series_id==point['seriesId'],
                    MacroObservation.observed_at==date.fromisoformat(point['period'])).with_for_update()).all()
                if previous:raise ValueError('EXCHANGE_UNEXPECTED_EXISTING_CORE')
                revision=digest({'runId':key,'businessSha256':digest(data),'point':point})
                core=MacroObservation(source='tushare',series_id=point['seriesId'],name=point['name'],region='CN',frequency='daily',
                    unit=point['unit'],seasonal_adjustment='NSA',observed_at=date.fromisoformat(point['period']),released_at=now,
                    revision=revision,value=core_value(point['value']),source_url=d['plan']['groupPolicy']['documentationUrl'])
                session.add(core);session.flush()
                bindings.append({'coreId':core.id,'seriesId':core.series_id,'period':point['period'],'value':point['value'],'revision':revision})
            row.details={**d,'phase':'completed','normalized':normalized,'coreBindings':bindings}
            row.status='completed';row.finished_at=now;row.row_count=len(bindings)
            batch=session.get(IngestionBatch,key);batch.status='completed';batch.finished_at=now;batch.row_count=len(bindings)
            from app.acquisition.adapters.plans import record_extension
            record_extension(session,row,now)
            return {'status':'completed','supplierHttp':0,'readOnly':False,'runId':key,'insertedRows':len(bindings)}

    def inventory(self,plan,now):
        state=self.inspect(plan,now);points=[]
        with self.factory() as session:
            for entry in state['documents']:
                if entry['phase']=='completed':points.extend(session.get(JobRun,entry['runId']).details['coreBindings'])
        return {'rawRows':len(points),'validVisibleRows':len(points),'invalidRows':0,'notVisibleRows':0,
                'distinctPeriods':len({p['period'] for p in points}),'scope':'frozen-plan-core-bindings'}
