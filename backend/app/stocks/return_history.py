"""供应商ROE/ROIC报告历史；累计比例不作单季差分或年化。"""
from collections import defaultdict
from decimal import Decimal,InvalidOperation
from math import isfinite
import re


SCREENER_ROIC_BASIS = 'tushare-fina-indicator-roic-reported-percent-latest-of-eight-fy'


def project_screener_roic(stock):
    """按结果表默认年报窗口，投影最近八个已披露 FY 中最新的非空百分数。"""
    history = stock.get('listHistory')
    reports = history.get('returns') if isinstance(history, dict) else None
    if not isinstance(reports, list):
        reports = []
    annual = [r for r in reports if isinstance(r, dict) and r.get('frequency') == 'FY'
              and isinstance(r.get('period'), str) and re.fullmatch(r'[0-9]{4}-12-31', r['period'])]
    annual = sorted(annual, key=lambda r: r['period'])[-8:]
    latest = next((r for r in reversed(annual)
                   if type(r.get('roic')) in (int, float) and isfinite(r['roic'])), None)
    amount = latest['roic'] if latest else None
    stock['roic'] = amount
    for key in ('researchValues', 'metricBasis', 'listMissingReasons'):
        if not isinstance(stock.get(key), dict):
            stock[key] = {}
    stock['researchValues']['roic'] = amount
    stock['metricBasis']['roic'] = SCREENER_ROIC_BASIS
    if latest:
        stock['listMissingReasons'].pop('roic', None)
    else:
        stock['listMissingReasons']['roic'] = '最近八个年报期缺少已披露的 fina_indicator.roic 百分数'


def _number(value):
    if value is None or isinstance(value,bool):
        return None
    try:
        exact=Decimal(str(value))
        converted=float(exact)
        return exact if exact.is_finite() and isfinite(converted) and (converted!=0 or exact==0) else None
    except (InvalidOperation,ValueError,TypeError,OverflowError):
        return None


def return_history(points,as_of):
    grouped=defaultdict(list)
    for point in points:
        if point.source_table!='core.financial_indicator' or point.report_type not in (None,'','1'):
            continue
        if point.end_date>as_of or (point.end_date.month,point.end_date.day) not in {(3,31),(6,30),(9,30),(12,31)}:
            continue
        if point.visible_at is not None and point.visible_at>as_of:
            continue
        grouped[point.end_date].append(point)
    reports=[]
    for period,items in sorted(grouped.items()):
        # 早于报告期的日期不可能是该期有效版本；有正常记录时隔离坏行，
        # 不让错误旧日期抹掉最新事实。未知日期仍不能断言版本先后。
        valid_or_unknown=[p for p in items if p.visible_at is None or p.visible_at>=period]
        if valid_or_unknown:
            items=valid_or_unknown
        invalid=any(p.visible_at is None or p.visible_at<period for p in items)
        if not invalid:
            announced=max(p.visible_at for p in items)
            items=[p for p in items if p.visible_at==announced]
            flag=max(str(p.values.get('update_flag') or '') for p in items)
            items=[p for p in items if str(p.values.get('update_flag') or '')==flag]
        else:
            announced=None
        reasons={};values={}
        for field in ('roe','roic'):
            observed={_number(p.values.get(field)) for p in items}
            value=next(iter(observed)) if len(observed)==1 and not invalid else None
            values[field]=float(value) if value is not None else None
            if value is None:
                reasons[field]='披露日期缺失或早于报告期，不能用报告期代替' if invalid else '同版本冲突或存在未知值' if len(observed)>1 else '缺少同报告期'+field.upper()
        premium=_number(Decimal(str(values['roe']))-Decimal(str(values['roic']))) if all(values[f] is not None for f in ('roe','roic')) else None
        if premium is None:
            reasons['premium']='缺少同报告期且口径有效的ROE/ROIC'
        # 同一批可见版本内逐字段核验，供财报 PK 优先使用已报告值。
        pk={}
        for field in ('profit_dedt','grossprofit_margin','netprofit_margin','quick_ratio','interestdebt'):
            observed={_number(p.values.get(field)) for p in items}
            if not invalid and len(observed)==1 and None not in observed:
                pk[field]=float(observed.pop())
        reports.append(dict(period=period.isoformat(),frequency='FY' if period.month==12 else 'YTD',
            announcedAt=announced.isoformat() if announced else None,**values,premium=float(premium) if premium is not None else None,
            reasons=reasons,sourceIds=sorted({str(p.source_record_key) for p in items if p.source_record_key}),
            **({'financialPk':pk} if pk else {})))
    return dict(asOf=as_of.isoformat(),basis='tushare-fina-indicator-reported-percent',reports=reports)


def disclosure_diagnostics(factory,symbols,as_of):
    """补回主仓库为计算安全而排除的无披露点，仅作缺失诊断。"""
    if factory is None or not symbols:
        return ()
    from sqlalchemy import select
    from app.db.models import FinancialIndicator
    from app.stocks.derived.inputs import FinancialPoint,_row_data
    with factory() as session:
        rows=session.scalars(select(FinancialIndicator).where(FinancialIndicator.ts_code.in_(symbols),
            FinancialIndicator.end_date<=as_of,FinancialIndicator.ann_date.is_(None),FinancialIndicator.f_ann_date.is_(None))).all()
        return tuple(FinancialPoint(r.end_date,None,r.report_type,_row_data(r),r.ts_code,
            'core.financial_indicator',f'core.financial_indicator:{r.id}') for r in rows)
