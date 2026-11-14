"""仅在只读事务中盘点扩展Core身份，不声明来源证据/发布就绪。"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.db.models.core import MacroObservation
from app.macro.extensions.config import ExtensionError, load_extension_config, selected_extension_groups
from app.macro.extensions.plan import build_extension_plan
from app.macro.extensions.store import ExtensionStore, execution_schema
from app.shared.readonly_database import open_readonly_factory


def _utc(value: datetime) -> datetime:
    # SQLite不保留时区；项目Core时间按UTC存储，不能解释成机器本地时区。
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _inventory(session, plan: dict, now: datetime) -> dict:
    counts = {'rawRows': 0, 'validVisibleRows': 0, 'invalidRows': 0,
        'notVisibleRows': 0, 'distinctPeriods': 0}
    periods = set()
    statement = select(MacroObservation).where(MacroObservation.series_id == plan['seriesId'])
    for row in session.scalars(statement.execution_options(yield_per=500)):
        counts['rawRows'] += 1
        fiscal = plan['groupId'] == 'governmentBonds'
        source_urls = {d['url'] for d in plan['documents']} if fiscal else {plan['sourceUrl']}
        valid = (row.source == plan['source'] and row.unit == plan['unit']
            and row.frequency == plan['frequency'] and row.source_url in source_urls
            and row.region == plan.get('region', 'CN') and row.seasonal_adjustment == 'NSA'
            and row.value.is_finite() and (fiscal or row.value > 0)
            and (row.valid_until is None or _utc(row.valid_until) > _utc(row.released_at)))
        if plan['groupId'] == 'nominalGdp':
            valid = valid and row.observed_at.day == 1 and row.observed_at.month in (1, 4, 7, 10)
            period = f'{row.observed_at.year:04}Q{(row.observed_at.month - 1) // 3 + 1}'
        else:
            period = row.observed_at.isoformat()
            if fiscal:
                valid = valid and row.observed_at.day == 1
        if not valid:
            counts['invalidRows'] += 1
        elif (_utc(row.released_at) > now
            or (row.valid_until is not None and _utc(row.valid_until) <= now)
            or not plan['startPeriod'] <= period <= plan['endPeriod']):
            counts['notVisibleRows'] += 1
        else:
            counts['validVisibleRows'] += 1
            periods.add(period)
    counts['distinctPeriods'] = len(periods)
    return counts


def preview_macro_extensions(settings: Settings, *, now: datetime | None = None, group_ids=None) -> dict:
    config = load_extension_config()  # 必须先校验配置，随后才允许数据库连接。
    if settings.data_backend != 'database' or not settings.database_url:
        raise ExtensionError('MACRO_EXTENSION_DATABASE_REQUIRED')
    checked_at = now if now is not None else datetime.now(timezone.utc)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ExtensionError('EXTENSION_PLAN_INVALID')
    checked_at = checked_at.astimezone(timezone.utc)
    groups = []
    from app.macro.exchange_summary_config import EXCHANGE_SUMMARY_GROUPS
    selected=selected_extension_groups(config,group_ids)
    plans={key:build_extension_plan(key,config,checked_at) for key in selected if key not in EXCHANGE_SUMMARY_GROUPS}
    # 执行账本不能用可能落后的只读副本作状态依据；主源仍强制只读事务。
    with open_readonly_factory(settings.database_url) as factory:
        problems = execution_schema(factory)
        with factory() as session:
            for key in selected:
                if key in EXCHANGE_SUMMARY_GROUPS:
                    from app.macro.exchange_summary_store import ExchangeSummaryStore,plan_from_database
                    try:
                        plan=plan_from_database(factory,config,key,checked_at)
                        store=ExchangeSummaryStore(factory)
                        execution=None if problems else store.inspect(plan,checked_at)
                        inventory=({'rawRows':0,'validVisibleRows':0,'invalidRows':0,'notVisibleRows':0,
                            'distinctPeriods':0,'scope':'frozen-plan-core-bindings'} if problems else store.inventory(plan,checked_at))
                        groups.append({'groupId':key,'collectionEnabled':config.groups[key].collectionEnabled,
                            'plan':plan,'inventory':inventory,'execution':execution,'readyForPublication':False,
                            'status':'inventory_only' if inventory['validVisibleRows'] else 'source_pending'})
                    except (ValueError,SQLAlchemyError,KeyError,TypeError):
                        groups.append({'groupId':key,'collectionEnabled':config.groups[key].collectionEnabled,
                            'plan':None,'inventory':None,'execution':None,'readyForPublication':False,
                            'status':'blocked','errorCode':'EXCHANGE_CALENDAR_OR_LEDGER_INVALID'})
                    continue
                plan=plans[key]
                from app.macro.extensions.config import OFFICIAL_MACRO_GROUPS
                if key in OFFICIAL_MACRO_GROUPS:
                    from app.macro.official_macro_store import OfficialMacroStore
                    store = OfficialMacroStore(factory)
                    execution = None if problems else store.inspect(plan, checked_at)
                    inventory = ({'rawRows':0,'validVisibleRows':0,'invalidRows':0,'notVisibleRows':0,
                                  'distinctPeriods':0,'scope':'frozen-plan-core-bindings'} if problems else
                                 store.inventory(plan, checked_at))
                    groups.append({'groupId':key,'collectionEnabled':config.groups[key].collectionEnabled,
                        'plan':plan,'inventory':inventory,'readyForPublication':False,
                        'status':'inventory_only' if inventory['validVisibleRows'] else 'source_pending',
                        'execution':execution})
                    continue
                from app.macro.fiscal_store import FiscalStore
                store = FiscalStore(factory) if key == 'governmentBonds' else ExtensionStore(factory)
                inventory = _inventory(session, plan, checked_at)
                groups.append({'groupId': key, 'collectionEnabled': config.groups[key].collectionEnabled,
                    'plan': plan, 'inventory': inventory, 'readyForPublication': False,
                    'status': 'inventory_only' if inventory['validVisibleRows'] else 'source_pending',
                    'execution': None if problems else store.inspect(plan, checked_at)})
    return {'status': 'preview_only', 'checkedAt': checked_at.isoformat(), 'groups': groups,
        'readOnly': True, 'supplierHttp': 0, 'executorImplemented': True,
        'physicalBudgetImplemented': True, 'physicalBudgetEnforced': False,
        'schemaReady': not problems, 'problems': problems,
        'executionReadConnection': 'primary-readonly',
        'publicationRequested': False,
        'planPersisted': any(group['execution'] is not None and group['execution']['runId'] is not None for group in groups)}
