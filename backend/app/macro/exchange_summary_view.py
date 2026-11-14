"""沪深具名范围对照；四个必要输入同日才计算，北交所单列。"""
from copy import deepcopy
from datetime import date,datetime,time,timedelta,timezone
from decimal import Decimal,localcontext
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.models.ops import JobRun
from app.macro.exchange_summary_config import EXCHANGE_SUMMARY_GROUPS
from app.macro.exchange_summary_normalize import normalize_exchange_response
from app.macro.exchange_summary_plan import SERIES,validate_exchange_plan
from app.macro.exchange_summary_store import JOB,ExchangeSummaryStore,run_key,safe_business
from app.macro.extensions.ingest import utc
from app.macro.extensions.plan import canonical,digest
from app.macro.extensions.store import FAILURE_CODES,instant

BASIS='normalization-visible-restated-history-not-strict-pit'
FORMULA='100*(SSE.rzye+SZSE.rzye)/(100000000*(SH_A.float_mv+SZ_A.float_mv))'
SCOPE='沪深交易所证券融资余额（可能含基金）/沪深A股流通市值；非纯A股融资比例，北交所余额单列'
CALIBRATED_SCOPE=SCOPE+'；深市缺项时仅使用供应商报告的两A板块，单位按官方样例标定'


def _day(value):
    if type(value) is not str or date.fromisoformat(value).isoformat()!=value:raise ValueError()
    return date.fromisoformat(value)


def _stamp(value,ceiling):
    if type(value) is not str:raise ValueError()
    at=instant(datetime.fromisoformat(value))
    if at.isoformat()!=value or not value.endswith('+00:00') or at>ceiling:raise ValueError()
    return at


def _text(value):
    if value==0:return '0'
    text=format(value,'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def evidence_hash(value):
    # 大合同分块；客户端每次仅需对一个具名目标或小型索引执行 SHA。
    groups={key:{'targets':[digest(t) for t in group['targets']],
                 'latestAttemptHash':digest(group['latestAttempt'])} for key,group in value['groups'].items()}
    proof={'calibrationHash':digest(value['calibration'])} if 'calibration' in value else {}
    return digest({k:value[k] for k in ('schemaVersion','algorithmVersion','basis','analysisDate','window','formula','scope','useScope')}
        | {'groups':groups,'monthlyHash':digest(value['monthly']),'missingPeriods':value['missingPeriods']} | proof)


def _receipt(plan,receipt,ceiling):
    if type(receipt) is not dict or set(receipt)!={'documentId','runId','retrievedAt','completedAt','businessData','businessSha256','normalized','coreBindings'}:
        raise ValueError()
    doc=next(d for d in plan['documents'] if d['id']==receipt['documentId'])
    if receipt['runId']!=run_key(plan,doc):raise ValueError()
    retrieved=_stamp(receipt['retrievedAt'],ceiling);completed=_stamp(receipt['completedAt'],ceiling)
    if retrieved>completed or retrieved<instant(datetime.fromisoformat(plan['releaseWindow']['startAt'])):raise ValueError()
    data=safe_business(receipt['businessData'],plan['limits']['maxResponseBytes'])
    normalized=normalize_exchange_response(plan,doc,data)
    if canonical(data)!=canonical(receipt['businessData']) or digest(data)!=receipt['businessSha256'] or normalized!=receipt['normalized']:raise ValueError()
    bindings=receipt['coreBindings']
    if type(bindings) is not list or len(bindings)!=len(normalized['points']):raise ValueError()
    for point,binding in zip(normalized['points'],bindings,strict=True):
        revision=digest({'runId':receipt['runId'],'businessSha256':digest(data),'point':point})
        if (type(binding) is not dict or set(binding)!={'coreId','seriesId','period','value','revision'}
            or type(binding['coreId']) is not int or binding['coreId']<=0
            or binding!={'coreId':binding['coreId'],'seriesId':point['seriesId'],'period':point['period'],
                'value':point['value'],'revision':revision}):raise ValueError()
    return {p['seriesId']:p['value'] for p in normalized['points']}


def _groups(groups,ceiling,end):
    if type(groups) is not dict or set(groups)!=EXCHANGE_SUMMARY_GROUPS:raise ValueError()
    values={};lookback=1;known_core=set()
    for key,group in groups.items():
        if type(group) is not dict or set(group)!={'targets','latestAttempt'} or type(group['targets']) is not list or len(group['targets'])>1488:
            raise ValueError()
        previous='';dates={}
        for target in group['targets']:
            if type(target) is not dict or set(target)!={'plan','documents'}:raise ValueError()
            p=target['plan'];validate_exchange_plan(p)
            if p['groupId']!=key or not previous<p['targetDate']<=end.isoformat():raise ValueError()
            previous=p['targetDate']
            if type(target['documents']) is not list or len(target['documents'])>len(p['documents']):raise ValueError()
            indexes=[];points={}
            for receipt in target['documents']:
                indexes.append(next(i for i,d in enumerate(p['documents']) if d['id']==receipt['documentId']))
                points.update(_receipt(p,receipt,ceiling))
                for binding in receipt['coreBindings']:
                    if binding['coreId'] in known_core:raise ValueError()
                    known_core.add(binding['coreId'])
            if indexes!=list(range(len(indexes))):raise ValueError()
            dates[p['targetDate']]=points
        latest=group['latestAttempt']
        if latest is None:
            if group['targets']:raise ValueError()
        else:
            if type(latest) is not dict or set(latest)!={'plan','documentId','runId','phase','startedAt','finishedAt','errorCode'}:raise ValueError()
            p=latest['plan'];validate_exchange_plan(p)
            lookback=max(lookback,p['groupPolicy']['lookbackPeriods'])
            doc=next(d for d in p['documents'] if d['id']==latest['documentId'])
            if p['groupId']!=key or p['targetDate']!=previous or group['targets'][-1]['plan']!=p or latest['runId']!=run_key(p,doc):raise ValueError()
            started=_stamp(latest['startedAt'],ceiling)
            if started<instant(datetime.fromisoformat(p['releaseWindow']['startAt'])):raise ValueError()
            phase=latest['phase']
            if phase not in ('pending','completed','failed'):raise ValueError()
            if phase=='pending':
                if latest['finishedAt'] is not None or latest['errorCode'] is not None:raise ValueError()
            else:
                if _stamp(latest['finishedAt'],ceiling)<started:raise ValueError()
                if (latest['errorCode'] not in FAILURE_CODES if phase=='failed' else latest['errorCode'] is not None):raise ValueError()
                receipts=group['targets'][-1]['documents']
                match=next((r for r in receipts if r['documentId']==doc['id']),None)
                if phase=='completed' and (match is None or match['completedAt']!=latest['finishedAt']):raise ValueError()
                if phase=='failed' and match is not None:raise ValueError()
        values[key]=dates
    return values,lookback


def build_exchange_summary_view(inputs):
    end=_day(inputs['analysisDate']);ceiling=datetime.combine(end+timedelta(days=1),time(),ZoneInfo('Asia/Shanghai')).astimezone(timezone.utc)-timedelta(microseconds=1)
    groups=deepcopy(inputs['groups']);values,lookback=_groups(groups,ceiling,end)
    calibration=deepcopy(inputs.get('calibration'))
    calibrated_date=None
    if calibration is not None:
        from app.macro.sz_market_calibration import proof_valid,stamp
        if not proof_valid(calibration) or stamp(calibration['normalizedAt'])>ceiling:raise ValueError()
        sample=calibration['sample'];parent=sample['plan']['parent'];calibrated_date=calibration['point']['period']
        receipts=[(t['plan'],r) for t in groups['aShareFloatMv']['targets'] for r in t['documents']
            if r['runId']==parent['runId']]
        if len(receipts)!=1:raise ValueError()
        p,r=receipts[0]
        if (p['fingerprint']!=parent['planFingerprint'] or p['targetDate']!=calibrated_date
            or stamp(sample['retrievedAt'])<stamp(r['completedAt'])
            or r['businessSha256']!=parent['businessSha256'] or r['normalized']['status']!='empty'
            or r['normalized']['points'] or r['coreBindings']
            or not any(d['id']==r['documentId'] and d['query']['params']=={'ts_code':'SZ_A','exchange':'SZ',
                'trade_date':calibrated_date.replace('-','')} for d in p['documents'])
            or any(b['coreId']==calibration['coreBinding']['coreId'] for g in groups.values()
                for t in g['targets'] for receipt in t['documents'] for b in receipt['coreBindings'])):raise ValueError()
        points=values['aShareFloatMv'].setdefault(calibrated_date,{})
        if SERIES['aShareFloatMv'][1] not in points:points[SERIES['aShareFloatMv'][1]]=calibration['point']['value']
    last=end.year*12+end.month-1
    def month(n):return f'{n//12:04}-{n%12+1:02}-01'
    monthly=[];selected=set()
    for index in range(last-lookback+1,last+1):
        label=month(index);candidates=[]
        for trade,points in values['marginBalances'].items():
            other=values['aShareFloatMv'].get(trade,{})
            if (trade[:7]==label[:7] and all(s in points for s in SERIES['marginBalances'][:2])
                and all(s in other for s in SERIES['aShareFloatMv'])):candidates.append(trade)
        row={'month':label,'tradeDate':None,'value':None,'reason':'MISSING_SAME_DAY_INPUTS',
            'marginBalanceYuan':None,'floatMvYiYuan':None,'sseBalanceYuan':None,'szseBalanceYuan':None,
            'bseBalanceYuan':None,'shFloatMvYiYuan':None,'szFloatMvYiYuan':None}
        if calibration is not None:row['szUnitBasis']=None
        if candidates:
            trade=max(candidates);selected.add(trade);p=values['marginBalances'][trade];q=values['aShareFloatMv'][trade]
            with localcontext() as context:
                context.prec=80
                a,b=(Decimal(p[s]) for s in SERIES['marginBalances'][:2]);x,y=(Decimal(q[s]) for s in SERIES['aShareFloatMv'])
                numerator=a+b;denominator=x+y
                with localcontext() as ratio_context:
                    ratio_context.prec=40
                    ratio=None if denominator<=0 else _text(100*numerator/(Decimal(100000000)*denominator))
            row.update(tradeDate=trade,value=ratio,reason=None if ratio is not None else 'NONPOSITIVE_DENOMINATOR',
                marginBalanceYuan=_text(numerator),floatMvYiYuan=_text(denominator),sseBalanceYuan=_text(a),szseBalanceYuan=_text(b),
                bseBalanceYuan=p.get(SERIES['marginBalances'][2]),shFloatMvYiYuan=_text(x),szFloatMvYiYuan=_text(y))
            if calibration is not None:
                native=any(t['plan']['targetDate']==trade and any(b['seriesId']==SERIES['aShareFloatMv'][1]
                    for r in t['documents'] for b in r['coreBindings']) for t in groups['aShareFloatMv']['targets'])
                row['szUnitBasis']='supplier-explicit' if native else 'official-example-cross-calibrated'
        monthly.append(row)
    for group in groups.values():
        latest=group['latestAttempt']['plan']['targetDate'] if group['latestAttempt'] else None
        group['targets']=[t for t in group['targets'] if t['plan']['targetDate'] in selected
            or t['plan']['targetDate']==latest or t['plan']['targetDate']==calibrated_date]
    value={'schemaVersion':'1.0','algorithmVersion':'exchange-summary-v1','basis':BASIS,'analysisDate':end.isoformat(),
        'window':{'startPeriod':month(last-lookback+1),'endPeriod':month(last),'requestedPeriods':lookback},
        'formula':FORMULA,'scope':SCOPE,'useScope':'personal-local-noncommercial','groups':groups,'monthly':monthly,
        'missingPeriods':[r['month'] for r in monthly if r['value'] is None]}
    if calibration is not None:
        value.update(schemaVersion='1.1',algorithmVersion='exchange-summary-v2',scope=CALIBRATED_SCOPE,calibration=calibration)
    return {**value,'evidenceHash':evidence_hash(value)}


def exchange_summary_view_valid(value):
    try:
        if type(value) is not dict:return False
        extra={'calibration'} if value.get('schemaVersion')=='1.1' else set()
        if set(value)!={'schemaVersion','algorithmVersion','basis','analysisDate','window','formula','scope','useScope','groups','monthly','missingPeriods','evidenceHash'}|extra:
            return False
        if any(len(g['targets'])>(50 if extra else 49) for g in value['groups'].values()):return False
        return canonical(value)==canonical(build_exchange_summary_view({'analysisDate':value['analysisDate'],
            'groups':value['groups'],**({'calibration':value['calibration']} if extra else {})}))
    except (ValueError,TypeError,KeyError,StopIteration,AttributeError,ArithmeticError,OverflowError):return False


def load_exchange_summary_view(factory,as_of):
    cutoff=instant(as_of);groups={key:{'targets':[],'latestAttempt':None} for key in sorted(EXCHANGE_SUMMARY_GROUPS)}
    store=ExchangeSummaryStore(factory);by_group={key:{} for key in groups}
    with factory() as session:
        rows=session.scalars(select(JobRun).where(JobRun.job_name.in_([JOB+g for g in groups]),JobRun.started_at<=cutoff)
            .order_by(JobRun.started_at,JobRun.id)).all()
        if not rows:return None
        ordered=[]
        for row in rows:
            d=store._run(session,row);ordered.append((d['plan']['targetDate'],d['plan']['documents'].index(d['document']),row,d))
        for _,__,row,d in sorted(ordered,key=lambda x:(x[0],x[1])):
            key=d['plan']['groupId'];target=by_group[key].setdefault(d['plan']['targetDate'],{'plan':deepcopy(d['plan']),'documents':[]})
            visible=row.finished_at is not None and utc(row.finished_at)<=cutoff
            phase=d['phase'] if visible else 'pending'
            groups[key]['latestAttempt']={'plan':deepcopy(d['plan']),'documentId':d['document']['id'],'runId':row.id,
                'phase':phase if phase in ('completed','failed') else 'pending','startedAt':utc(row.started_at).isoformat(),
                'finishedAt':utc(row.finished_at).isoformat() if visible else None,'errorCode':row.error_message if phase=='failed' else None}
            if phase=='completed':
                data,retrieved=store._raw(session,row)
                if retrieved<=cutoff:
                    target['documents'].append({'documentId':d['document']['id'],'runId':row.id,'retrievedAt':retrieved.isoformat(),
                        'completedAt':utc(row.finished_at).isoformat(),'businessData':deepcopy(data),'businessSha256':digest(data),
                        'normalized':deepcopy(d['normalized']),'coreBindings':deepcopy(d['coreBindings'])})
    for key in groups:groups[key]['targets']=[v for _,v in sorted(by_group[key].items())]
    from app.macro.sz_market_calibration import CalibrationStore
    calibration=CalibrationStore(factory).load(cutoff)
    return build_exchange_summary_view({'analysisDate':cutoff.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat(),
        'groups':groups,**({'calibration':calibration} if calibration is not None else {})})
