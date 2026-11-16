import base64
import hashlib
import json
import re
from dataclasses import asdict, replace
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select, text

from app.acquisition.adapters.macro import read_macro_evidence
from app.acquisition.availability.published import read_published
from app.acquisition.availability.stored import read_stored
from app.acquisition.catalog import load_bindings
from app.acquisition.contracts import QueryError, UnknownResource, Assessment, Availability, Evidence, StateKey, StatusItem, StatusPage
from app.acquisition.periods import bounds, covers, contains
from app.acquisition.repository import StateRepository, scope_id
from app.acquisition.rules import classify, classify_derived
from app.db.models import DataAcquisitionState

ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=4)
def catalog_entries(root):
    return {b.entry_id for b in load_bindings(root)}


def configured(root, binding):
    from app.config_paths import CONFIG_ROOT
    config = root/'config' if (root/'config').is_dir() else CONFIG_ROOT
    kind, entry = binding.subject_kind, binding.entry_id
    from app.acquisition.adapters.aliases import SERIES
    aliases = [subject for (key,subject) in SERIES if key == entry]
    if aliases:
        return aliases
    if entry == 'market-sz-summary':
        return ['SZSE']
    if kind == 'security':
        data = json.loads((config/'stocks/universes/representative-50.json').read_text(encoding='utf-8'))
        return [r['tsCode'] for r in data['symbols']]
    if entry in ('index-basic','index-weights','etf-basic'):
        targets = json.loads((config/'macro/enterprise-targets.json').read_text(encoding='utf-8'))['targets']
        kind = 'etf' if entry=='etf-basic' else 'index'
        codes = [row['code'] for row in targets if row['kind']==kind]
        if entry=='index-basic':
            from app.portfolio.reference_catalog import TARGETS
            codes.extend(TARGETS)
        return codes
    if kind == 'instrument':
        data = json.loads((config/'market/market-data.json').read_text(encoding='utf-8'))
        api = {'index-daily':'index_daily','index-global':'index_global','etf-daily':'fund_daily','etf-shares':'fund_daily'}.get(entry)
        if entry in ('commodity-contracts','commodity-mapping'):
            return [r['code'] for r in data.get('commodities',{}).get('assets',[])]
        return [r['assetCode'] for r in data['assets'] if r['apiName'] == api]
    if entry == 'external-macro' and kind == 'series':
        return []
    if kind == 'series':
        return [binding.item_id or entry]
    if kind == 'product':
        return [{'portfolio-inputs':'portfolio','macro-spreads':'macro-insights','macro-enterprise':'macro-transmission'}.get(entry,entry)]
    if entry == 'stock-classification':
        return ['SW2021']
    if kind == 'exchange' and entry == 'trade-calendar':
        return ['SSE','SZSE','BSE']
    return ['sina-finance'] if entry == 'news' else []


def _cursor(query, last=None):
    filters = {**asdict(query),'cursor':None}
    digest = hashlib.sha256(json.dumps(filters,sort_keys=True).encode()).hexdigest()
    if last:
        return base64.urlsafe_b64encode(json.dumps([digest,list(asdict(last).values())]).encode()).decode()
    if not query.cursor:
        return None
    try:
        if len(query.cursor) > 2048:
            raise ValueError
        saved, key = json.loads(base64.b64decode(query.cursor,altchars=b'-_',validate=True))
        if saved != digest or not isinstance(key,list) or len(key)!=5 or not all(isinstance(x,str) for x in key):
            raise ValueError
        return StateKey(*key)
    except (ValueError,TypeError,UnicodeError):
        raise QueryError('游标无效或查询条件已改变') from None


def assess_keys(session, keys, context, *, now, root=ROOT):
    # 同一次读取共享依赖和证据。
    bindings = {(b.entry_id,b.item_id):b for b in load_bindings(root)}
    from app.acquisition.availability.members import member_reader
    subjects_for, enterprise_inputs = member_reader(session,context)
    required = set(keys)
    children = {}
    def expand(key):
        binding = bindings[key.entry_id,key.item_id]
        deps = []
        for dep in binding.dependencies:
            for child in bindings.values():
                if child.entry_id != dep or (child.item_id and (dep,'') in bindings):
                    continue
                deps.extend(StateKey(dep,child.item_id,child.subject_kind,subject,key.scope_kind)
                            for subject in subjects_for(key,child))
        if key.entry_id == 'macro-enterprise':
            deps.extend(enterprise_inputs(key))
        for child_key in deps:
            if child_key not in required:
                required.add(child_key)
                expand(child_key)
        children[key] = deps
    for key in keys:
        expand(key)
    evidence = StateRepository(session).read(tuple(required))
    legacy_keys = tuple(k for k in required if k.subject_kind == 'series' or
                        k.entry_id in ('stock-income','stock-balance','stock-cashflow'))
    for key, old in read_macro_evidence(session,legacy_keys,now=now).items():
        new = evidence.get(key)
        if (not new or old.status in ('paused','running') and key.subject_kind=='series' or
            old.checked_at and (not new.checked_at or old.checked_at > new.checked_at)
            and (new.status != 'failed' or old.status == 'failed' or scope_id(new.scope) == scope_id(old.scope))):
            evidence[key] = old
    values = (read_published(session,required,context) if context.view == 'published' else
              read_stored(session,required,now=now,period=context.period))
    from app.acquisition.adapters.targets import APIS, target_status
    for key in required:
        if key.entry_id not in APIS or key.scope_kind!='routine':
            continue
        old, value = target_status(session,key,now)
        new = evidence.get(key)
        if old and (not new or new.status not in ('failed','running') and old.checked_at and
                    (not new.checked_at or old.checked_at>new.checked_at)):
            evidence[key] = old
        if context.view=='stored' and values[key].kind=='unknown':
            values[key] = value
    from app.config_paths import CONFIG_ROOT
    config_root = root/'config' if (root/'config').is_dir() else CONFIG_ROOT
    source_configs = {}
    def paused(key):
        file, section = None, None
        if key.entry_id=='news':
            file = 'news/news-collector.json'
        elif key.entry_id.startswith('commodity-'):
            file,section = 'market/market-data.json','commodities'
        elif key.entry_id=='etf-shares':
            file,section = 'market/market-data.json','etfHeatmap'
        elif key.entry_id=='security-region':
            file,section = 'market/market-data.json','region'
        elif key.entry_id=='stock-daily-basic' and key.item_id=='limit_status':
            file,section = 'market/market-data.json','pulse'
        elif key.entry_id in ('index-daily','index-global'):
            file = 'market/market-data.json'
        if not file:
            return False
        if file not in source_configs:
            source_configs[file] = json.loads((config_root/file).read_text(encoding='utf-8'))
        config = source_configs[file]
        return config.get('enabled') is False or section and config.get(section,{}).get('enabled') is False
    security_keys = {k.subject_key for k in required if k.subject_kind=='security'}
    known_securities = set()
    if security_keys:
        from app.db.models import Security
        pool_binding = next(b for b in bindings.values() if b.subject_kind=='security')
        known_securities.update(configured(root,pool_binding))
        candidates = security_keys - known_securities
        if candidates:
            known_securities.update(session.scalars(select(Security.ts_code).where(Security.ts_code.in_(candidates))))
    items = {}
    def assess(key):
        if key in items:
            return items[key]
        proof = evidence.get(key,Evidence('unrecorded',reason='没有可核对的执行记录'))
        binding = bindings[key.entry_id,key.item_id]
        value = values[key]
        if proof.checked_at and proof.checked_at > now:
            proof = Evidence('unrecorded',reason='执行时间晚于查询时间')
        if value.kind == 'missing' and proof.status == 'success' and proof.scope.get('confirmedEmpty') is True:
            if context.view == 'stored' and not context.period:
                value = Availability('confirmed_empty',reason='已完整查询该范围，返回零条记录')
        if value.source and proof.scope.get('source') and value.source != proof.scope['source']:
            value = replace(value,kind='unknown',reason='数据与执行记录的来源不一致')
        if context.period and value.kind == 'available' and not contains(context.period,value.period):
            value = replace(value,kind='unknown',reason='数据期与请求范围尚未核对')
        if context.period and proof.status in ('success','failed') and not covers(proof.scope,value.period,context.period):
            proof = Evidence('unrecorded',reason='已有执行记录未证明所选数据期',scope=proof.scope)
        if (key.subject_kind=='security' and key.subject_key not in known_securities and
                value.kind=='missing' and proof.status=='unrecorded'):
            value = Availability('not_applicable',reason='证券未登记，当前来源覆盖范围待确认')
        if paused(key):
            proof = replace(proof,status='paused',reason='来源配置已暂停')
        result = classify(value,proof)
        if binding.source_adapter == 'derived' and proof.status not in ('paused','running'):
            deps = tuple(assess(k).assessment for k in children[key])
            if len(set(binding.dependencies)) != len({k.entry_id for k in children[key]}):
                deps += (Assessment('excluded','需核对聚合范围中的实际成员'),)
            result = classify_derived(value,deps)
        if key.entry_id == 'macro-enterprise' and value.kind == 'available' and proof.status not in ('paused','running'):
            data = [classify(Availability('available'),evidence.get(k,Evidence('unrecorded')))
                    for k in children[key]]
            if not data:
                data = [Assessment('excluded','未找到计算样本的报表回执')]
            result = classify_derived(value,tuple([result,*data]))
        name = binding.name
        items[key] = StatusItem(key,name,replace(context,version=value.version or context.version),proof,value,result)
        return items[key]
    return [assess(key) for key in keys]


class StatusService:
    def __init__(self, factory, *, root=ROOT, clock=None):
        self.factory, self.root = factory, root
        self.clock = clock or (lambda:datetime.now(UTC))

    def query(self, request):
        ctx = request.context
        if ctx.period:
            bounds(ctx.period)
        if request.scope_kind not in ('routine','historical') or ctx.view not in ('stored','published'):
            raise QueryError('视图或采集范围无效')
        if ctx.view == 'stored' and (ctx.product or ctx.version) or ctx.view == 'published' and not ctx.product:
            raise QueryError('发布视图须指定产品，入库视图不接受产品或版本')
        if not 1 <= request.limit <= 100 or not (request.entry_ids or request.subject_keys):
            raise QueryError('请指定目录条目或对象，分页大小必须在 1 至 100 内')
        if len(request.entry_ids)>20 or len(request.subject_keys)>100 or len(request.item_ids)>100:
            raise QueryError('筛选数量超过限制')
        if any(not re.fullmatch(r'[\w.:-]{1,160}',s) for s in request.subject_keys):
            raise QueryError('对象代码无效')
        if set(request.entry_ids) - catalog_entries(self.root):
            raise UnknownResource('目录条目不存在')
        all_bindings = load_bindings(self.root)
        if request.subject_kind and request.subject_kind not in {b.subject_kind for b in all_bindings}:
            raise QueryError('对象类型无效')
        allowed = [b for b in all_bindings if (not request.entry_ids or b.entry_id in request.entry_ids)
                   and (not request.item_ids or b.item_id in request.item_ids)
                   and (not request.subject_kind or b.subject_kind == request.subject_kind)]
        if request.item_ids and set(request.item_ids) - {b.item_id for b in allowed}:
            raise UnknownResource('条目内指标不存在')
        if request.entry_ids and len(allowed)*len(request.subject_keys)>100:
            raise QueryError('展开后的条目与对象组合超过 100')
        after = _cursor(request)
        now = self.clock()
        with self.factory() as session, session.begin():
            session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            candidates = set()
            for b in allowed:
                subjects = request.subject_keys or configured(self.root,b)
                if not request.subject_keys:
                    # 已记账的对象与当前配置取并集，已移出股票池的对象仍可分页核对。
                    stmt = select(DataAcquisitionState.subject_key).filter_by(entry_id=b.entry_id,item_id=b.item_id,
                        subject_kind=b.subject_kind,scope_kind=request.scope_kind)
                    if after and (b.entry_id,b.item_id,b.subject_kind) == (after.entry_id,after.item_id,after.subject_kind):
                        stmt = stmt.where(DataAcquisitionState.subject_key > after.subject_key)
                    subjects = sorted(set(subjects) | set(session.scalars(
                        stmt.order_by(DataAcquisitionState.subject_key).limit(request.limit+1))))
                for subject in subjects:
                    if b.subject_kind == 'security' and not re.fullmatch(r'[0-9]{6}\.(?:SZ|SH|BJ)',subject):
                        if request.entry_ids and all(x.subject_kind=='security' for x in allowed):
                            raise QueryError('证券代码应包含六位数字和交易所后缀')
                        continue
                    if b.subject_kind == 'series' and b.entry_id != 'external-macro' and subject != (b.item_id or b.entry_id):
                        continue
                    key = StateKey(b.entry_id,b.item_id,b.subject_kind,subject,request.scope_kind)
                    if b.subject_kind == 'product' and subject not in configured(self.root,b):
                        continue
                    if not after or key > after:
                        candidates.add(key)
            if len(request.subject_keys) > 1 and len(candidates) > 100:
                raise QueryError('展开后的条目与对象组合超过 100')
            ordered = sorted(candidates)
            page = ordered[:request.limit]
            items = assess_keys(session,page,ctx,now=now,root=self.root)
            return StatusPage('1.0',now,items,_cursor(request,page[-1]) if len(ordered)>len(page) else None)
