"""将同一事务中的有限Core输入转换成企业计算合同；不产生数据库写入。"""
from sqlalchemy import select

from app.db.models import Security, IncomeStatement, BalanceSheet, CashflowStatement, Classification, ClassificationMember
from app.macro.enterprise import EnterpriseInputs, FinancialRecord, IndustryInput, IndustryLabelInput, SecurityInput
from app.stocks.api.universe import UniverseSelection


def resolve_enterprise_selection(factory, settings) -> UniverseSelection:
    """只在显式all时读取当前在市成员；缺财报不能从候选范围消失。"""
    from app.stocks.api.collection import DailySyncRepository
    from app.stocks.api.universe import UniverseResolver
    return UniverseResolver(active_symbols=DailySyncRepository(factory).active_symbols).resolve(
        settings.macro_enterprise_universe, universe_file=settings.macro_enterprise_universe_file)


def load_enterprise_inputs(factory, selection: UniverseSelection) -> EnterpriseInputs:
    symbols=selection.symbols
    with factory() as session:
        securities=tuple(SecurityInput(row.ts_code,row.list_date) for row in session.scalars(
            select(Security).where(Security.ts_code.in_(symbols)).order_by(Security.ts_code)))
        records=[]
        for table,model in (('income',IncomeStatement),('balance',BalanceSheet),('cashflow',CashflowStatement)):
            for row in session.scalars(select(model).where(model.ts_code.in_(symbols)).order_by(model.id)):
                records.append(FinancialRecord(table,row.ts_code,row.end_date,row.ann_date,row.f_ann_date,
                    row.report_type,row.comp_type,row.update_flag,dict(row.data),f'core.{model.__tablename__}:{row.id}'))
        nodes=list(session.scalars(select(Classification).where(Classification.provider=='tushare',Classification.source=='SW2021')))
        by_id={row.id:row for row in nodes};by_code={row.classification_id:row for row in nodes}
        members=list(session.scalars(select(ClassificationMember).where(ClassificationMember.ts_code.in_(symbols)).order_by(ClassificationMember.id)))
        industries=[]
        for member in members:
            node=by_id.get(member.classification_id)
            if node is None or node.level!=3:
                continue
            seen=set()
            while node is not None and node.level!=1:
                if node.classification_id in seen:
                    node=None;break
                seen.add(node.classification_id)
                parent=by_code.get(node.parent_id)
                node=parent if parent is not None and parent.level==node.level-1 else None
            industries.append(IndustryInput(member.ts_code,node.classification_id if node else None,
                member.effective_from,member.effective_to,f'core.classification_member:{member.id}'))
    labels=tuple(IndustryLabelInput(row.classification_id,row.name,f'core.classification:{row.id}') for row in nodes if row.level==1)
    return EnterpriseInputs(selection.version,tuple(symbols),securities,tuple(records),tuple(industries),industry_labels=labels)


def load_enterprise_loss_inputs(factory,inputs,as_of,policy):
    """仅normal/audit显式装配；预检不读取市值，不扩大采集范围。"""
    from dataclasses import replace
    from datetime import date, timedelta
    from app.db.models import DailyBasic, TradeCalendar
    from app.macro.enterprise import MarketValueInput, TradingDayInput, _previous, _visible
    from app.macro.enterprise_loss import valuation_anchor, valuation_day
    observed=[r.end_date for r in inputs.financials if r.end_date<=as_of and _visible(r) is not None and _visible(r)<=as_of]
    periods=[]
    current=date(as_of.year,((as_of.month-1)//3)*3+1,1)-timedelta(days=1)
    while observed and current>=min(observed) and len(periods)<policy.max_quarters:
        periods.append(current);current=_previous(current)
    anchors=[valuation_anchor(p) for p in periods if valuation_anchor(p)<as_of]
    calendar={};used={};dates=set()
    with factory() as session:
        if anchors:
            for row in session.scalars(select(TradeCalendar).where(TradeCalendar.exchange=='SSE',
                TradeCalendar.cal_date>min(anchors),TradeCalendar.cal_date<=as_of).order_by(TradeCalendar.cal_date)):
                calendar.setdefault(row.cal_date,[]).append(TradingDayInput(row.cal_date,row.is_open,f'core.trade_calendar:{row.id}'))
        for period in periods:
            day,_,rows=valuation_day(period,as_of,calendar)
            if day: dates.add(day)
            for row in rows: used[row.source_key]=row
        caps=tuple(MarketValueInput(row.ts_code,row.trade_date,row.total_mv,f'core.daily_basic:{row.id}')
            for row in session.scalars(select(DailyBasic).where(DailyBasic.ts_code.in_(inputs.symbols),
                DailyBasic.trade_date.in_(dates)).order_by(DailyBasic.ts_code,DailyBasic.trade_date))) if dates else ()
    return replace(inputs,loss_enabled=True,market_values=caps,trading_days=tuple(sorted(used.values(),key=lambda r:(r.day,r.source_key))))
