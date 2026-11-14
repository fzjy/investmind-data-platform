"""仅按报告期组织本次观察的主营分项，不证明历史披露可见性。"""
from collections import defaultdict
from decimal import Decimal,InvalidOperation
from math import isfinite
import re
from app.portfolio.api.dividends import distribution_date


def _number(value):
    if value is None or value=='' or isinstance(value,bool):return None
    try:
        v=Decimal(str(value));f=float(v)
        return v if v.is_finite() and isfinite(f) and (f!=0 or v==0) else None
    except (InvalidOperation,TypeError,ValueError,OverflowError):return None



# 同一集合内的标签位于相同地理层级。供应商允许自由文本，未知组合保留明细。
DOMESTIC = {'国内', '境内', '中国', '中国境内', '中国大陆', '中国大陆地区', '国内地区', '境内地区',
            '国内销售', '境内市场', '境内小计'}
OVERSEAS = {'国外', '境外', '海外', '国际', '境外地区', '国外地区', '海外地区', '国外销售',
            '境外市场', '境外小计', '中国大陆地区以外的国家和地区', '其他国家或地区'}
PROVINCES = set('北京 上海 天津 重庆 河北 山西 辽宁 吉林 黑龙江 江苏 浙江 安徽 福建 江西 山东 河南 湖北 湖南 广东 海南 四川 贵州 云南 陕西 甘肃 青海 台湾 内蒙古 广西 西藏 宁夏 新疆 香港 澳门'.split())
COUNTRIES = set('日本 韩国 美国 加拿大 英国 法国 德国 意大利 西班牙 俄罗斯 澳大利亚 巴西 印度 印度尼西亚 越南 新加坡 马来西亚 泰国 墨西哥 南非'.split())
PRODUCT = re.compile(r'产品|业务|机械|设备|系统|材料|服务|药品|收入|收益|损益|酒|配件|项目|电池|工程|模块|软件|其他品种|解决方案')
REGION_FAMILIES = [
    re.compile(r'(华东|华中|华南|华北|西南|西北|东北)(?:地区|区域)?'),
    re.compile(r'([东西南北中])部(?:片区|大区|区域|地区)'),
    re.compile(r'([东西南北中])区'),
]


def region_basis(names):
    labels = set(names)
    inside, outside = labels & DOMESTIC, labels & OVERSEAS
    if len(inside) > 1 or len(outside) > 1:
        return None
    if inside and (labels - inside <= COUNTRIES or labels - inside == outside):
        return 'international' if labels - inside else None
    local = labels - outside
    provinces = {re.sub(r'(?:[省市]|地区)$', '', n) for n in local}
    is_local = bool(local) and provinces <= PROVINCES and len(provinces) == len(local)
    for family in REGION_FAMILIES:
        matches = [family.fullmatch(n) for n in local]
        if matches and all(matches) and len({m[1] for m in matches}) == len(local):
            is_local = True
    if is_local:
        return 'international' if outside else 'domestic'
    if labels and labels <= COUNTRIES:
        return 'international'
    return None


def classify_group(kind, labels):
    names = [name for name in labels if name != '合计特别调整']
    if not names or '' in names:
        return 'other', None
    if kind == 'D':
        basis = region_basis(names)
        return ('region', basis) if basis else ('other', None)
    geographic = DOMESTIC | OVERSEAS | PROVINCES | COUNTRIES
    mixed = re.compile(r'地区|片区|区域|大区|华[东西南北中]|东北|西南|西北|[东西南北中]部|渠道|直销|经销|线上|线下|批发|零售')
    if any(mixed.search(n) or any(n.startswith(place) for place in geographic) for n in names):
        return 'other', None
    if all(PRODUCT.search(n) or n == '其他' for n in names):
        return 'product', None
    return 'other', None


def composition_groups(kind, records, query_complete, currency):
    root_label = '产品' if kind == 'P' else '地区'
    by_path = defaultdict(list)
    children = defaultdict(set)
    for name in records:
        path = tuple(part.strip() for part in re.split('[:：]', name or ''))
        if path[0] == root_label:
            path = path[1:]
        by_path[path].append(name)
        for depth in range(len(path)):
            children[path[:depth]].add(path[:depth + 1])

    groups = []
    for path in sorted(children):
        def disclosed(node):
            originals = by_path.get(node, [])
            if len(originals) != 1:
                return None, None
            return originals[0], records[originals[0]]

        parent_name, parent_value = disclosed(path)
        members, amounts = [], []
        for child in sorted(children[path]):
            original, amount = disclosed(child)
            amounts.append(amount)
            members.append(dict(path=list(child), name=original, revenue=float(amount) if amount is not None else None, ratio=None))
        dimension, basis = classify_group(kind, [m['path'][-1] for m in members])
        reasons = []
        if not query_complete or not currency:
            reasons.append('查询未完成或币种未知')
        if parent_value is None or parent_value <= 0:
            reasons.append('缺少可核实的正数父级金额')
        if dimension == 'other':
            reasons.append('直属分项的分类维度或互斥关系未核实')
        if any(value is None or value < 0 for value in amounts):
            reasons.append('分项含负数、冲突或缺失金额')
        elif parent_value is not None and sum(amounts) != parent_value:
            reasons.append('直属分项之和与披露合计不符')
        if not reasons:
            for member, amount in zip(members, amounts, strict=True):
                member['ratio'] = float(amount / parent_value * 100)
        groups.append(dict(path=list(path), dimension=dimension, regionBasis=basis,
                           status='details' if reasons else 'verified', denominatorName=parent_name,
                           denominatorRevenue=float(parent_value) if parent_value is not None else None,
                           members=members, reasons=reasons))
    return groups


def project_business(evidence):
    grouped=defaultdict(list)
    for row in evidence['rows']:
        kind=row['businessType']
        if kind not in ('P','D'):raise ValueError('BUSINESS_TYPE_INVALID')
        period=distribution_date(row['end_date'])
        if period is None or period>evidence['sourceAsOf']:raise ValueError('BUSINESS_PERIOD_INVALID')
        currency=row.get('curr_type')
        currency=currency.strip().upper() if isinstance(currency,str) else None
        if not currency or not re.fullmatch('[A-Z]{3}',currency):currency=None
        grouped[(period,kind,currency)].append(row)
    reports=[]
    for (period,kind,currency),rows in sorted(grouped.items(),key=lambda p:(p[0][0],p[0][1],p[0][2] or '')):
        by_name=defaultdict(list)
        for row in rows:
            name=row.get('bz_item')
            name=name.strip() if isinstance(name,str) and name.strip() else None
            by_name[name].append(row)
        items=[];records={};reasons=['历史披露及修订时点未核验']
        if not currency:reasons.append('币种缺失或代码无效，不计算占比或跨期金额比较')
        if not evidence['queryComplete']:reasons.append('请求结束状态未获明确证明，仅为已观察分项')
        for name,versions in sorted(by_name.items(),key=lambda p:p[0] or ''):
            def version(v):
                value=v.get('update_flag')
                if value in (None,''):return ''
                if isinstance(value,bool) or value not in (0,1,'0','1'):raise ValueError('BUSINESS_VERSION_INVALID')
                return str(int(value))
            flag=max(version(v) for v in versions)
            versions=[v for v in versions if version(v)==flag]
            values={};notes=[]
            for field,label in [('bz_sales','收入'),('bz_profit','利润'),('bz_cost','成本')]:
                candidates={_number(v.get(field)) for v in versions}
                value=next(iter(candidates)) if len(candidates)==1 else None
                values[field]=value
                if len(candidates)>1:notes.append(label+'同版本冲突')
            revenue=values['bz_sales']
            if name is None:notes.append('分项名称缺失，不纳入构成分母')
            if revenue is None:notes.append('收入缺失或无法安全表示，不纳入构成分母')
            records[name]=revenue
            items.append(dict(name=name,revenue=float(revenue) if revenue is not None else None,
                profit=float(values['bz_profit']) if values['bz_profit'] is not None else None,
                cost=float(values['bz_cost']) if values['bz_cost'] is not None else None,ratio=None,
                **({'reason':'；'.join(notes)} if notes else {})))
        groups=composition_groups(kind,records,evidence['queryComplete'],currency)
        reports.append(dict(period=period,type=kind,currency=currency,items=items,
            positiveRevenueTotal=None,groups=groups,queryComplete=evidence['queryComplete'],reasons=reasons))
    return {key:evidence[key] for key in ('sourceAsOf','capturedAt','sourceBaseVersion')}|dict(compositionVersion=2,visibility='report-period-only',historyComplete=False,reports=reports)


def load_business_histories(factory,symbols,as_of):
    from app.stocks.business_supply import load_business_evidence
    return {symbol:project_business(evidence) for symbol,evidence in load_business_evidence(factory,symbols,as_of).items()}
