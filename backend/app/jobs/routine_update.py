"""一次手动日常运行；复用子任务检查点，未启用系统调度。"""
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime

from app.config import Settings
from app.content.announcement_runtime import announcement_policy, run_announcements
from app.stocks.update.runtime import run_stock_step
from app.macro.jobs.macro_catchup import run_macro_catchup
from app.macro.jobs.macro_catchup_plan import read_macro_catchup_plan
from app.macro.jobs.macro_sync import MacroPublishReport
from app.jobs.run_errors import JobConfigurationError, JobLockedError
from app.market.data.runtime import run_market_step
from app.market.pulse.runtime import run_pulse_step
from app.market.region.runtime import run_region_step
from app.market.etf.runtime import run_etf_step
from app.market.commodities.runtime import run_commodity_step
from app.jobs.supplier_job_pacing import SupplierJobPacer
from app.macro.jobs.macro_extension_runtime import run_macro_extensions
from app.macro.extensions.config import load_extension_config
from app.portfolio.routine import portfolio_routine_config, run_portfolio_step
from app.stocks.financial_supply import config as financial_supply_config
from app.stocks.financial_runtime import run_financial_step
from app.stocks.forecast_details import run_forecast_step
from app.stocks.financial_supply import forecast_policy
from app.stocks.valuation_supply import valuation_config as valuation_supply_config, run_valuation_step
from app.stocks.business_supply import business_config as business_supply_config,run_business_step




def _publication_status(value: dict) -> str | None:
    publication = value.get('publication')
    if not isinstance(publication, dict):
        return None
    if publication.get('status') == 'failed' and any(
        q.get('severity') == 'blocking' and q.get('passed') is False for q in publication.get('quality', ())):
        return 'blocked'
    return publication.get('status')


def _step_ready(value: dict, *, macro: bool, publish: bool) -> bool:
    if value['status'] == 'not_selected':
        return True
    ready = (value.get('activeCoverageCompleted') is True
        and value['status'] in {'completed', 'partial', 'skipped'}) if macro else (
        value['status'] == 'completed' and value.get('publishReady') is True)
    return bool(ready and (not publish or _publication_status(value) == 'completed'))


def run_routine_update(settings: Settings, *, macro_rebuild: Callable[[Settings], MacroPublishReport],
    dry_run: bool = False, publish: bool = False, now: datetime | None = None, only_portfolio: bool = False) -> dict:
    instant = now if now is not None else datetime.now(UTC)
    if instant.utcoffset() is None:
        raise ValueError('aware clock required')
    if settings.data_backend != 'database' or not settings.database_url:
        raise ValueError('database mode required')
    if only_portfolio:
        settings = settings.model_copy(update={field:False for field in (
            'routine_stocks_enabled','routine_macro_enabled','routine_market_enabled','routine_market_pulse_enabled',
            'routine_region_enabled','routine_etf_heatmap_enabled','routine_commodities_enabled')})
    extensions_selected = not only_portfolio and any(group.collectionEnabled for group in load_extension_config().groups.values())
    portfolio_selected = portfolio_routine_config()['enabled']
    financial_policy = {} if only_portfolio else financial_supply_config()
    financial_selected = financial_policy.get('enabled', False)
    forecast_config = forecast_policy() if financial_selected and settings.routine_stocks_enabled else {}
    forecast_selected = forecast_config.get('enabled', False)
    notice_policy = announcement_policy() if settings.routine_stocks_enabled else {}
    notices_selected = notice_policy.get('enabled', False)
    valuation_selected = not only_portfolio and valuation_supply_config()['enabled']
    business_selected = not only_portfolio and business_supply_config()['enabled']
    if not any((settings.routine_stocks_enabled, settings.routine_macro_enabled, settings.routine_market_enabled,
                settings.routine_market_pulse_enabled, settings.routine_region_enabled, settings.routine_etf_heatmap_enabled, settings.routine_commodities_enabled, extensions_selected, portfolio_selected, financial_selected, valuation_selected,business_selected)):
        raise ValueError('select at least one routine group')
    if settings.routine_stocks_enabled and (
        settings.sync_universe != 'representative-50' or settings.sync_universe_file is not None):
        raise ValueError('fixed representative-50 pool required')
    instant = instant.astimezone(UTC)
    rpm = settings.tushare_vip_requests_per_minute if settings.tushare_mode == 'vip' else settings.tushare_standard_requests_per_minute
    pacer = SupplierJobPacer(rpm) if (settings.routine_market_enabled or settings.routine_market_pulse_enabled or settings.routine_region_enabled or settings.routine_etf_heatmap_enabled or settings.routine_commodities_enabled or extensions_selected or portfolio_selected or financial_selected or valuation_selected or business_selected) and not dry_run else None
    def paced(function, *args, **kwargs):
        with pacer.scope() if pacer is not None else nullcontext():
            return function(*args, **kwargs)
    stocks, market, macro = {'status': 'not_selected'}, {'status': 'not_selected'}, {'status': 'not_selected'}
    pulse = {'status': 'not_selected'}
    region = {'status': 'not_selected'}
    etf = {'status': 'not_selected'}
    commodities = {'status': 'not_selected'}
    extensions = {'status': 'not_selected'}
    portfolio = {'status': 'not_selected'}
    financial = {'status': 'not_selected'}
    forecast = {'status': 'not_selected'}
    valuation = {'status': 'not_selected'}
    business = {'status': 'not_selected'}
    if settings.routine_stocks_enabled:
        try:
            stocks = paced(run_stock_step, settings, now=instant, dry_run=dry_run, publish=publish)
        except JobConfigurationError:
            stocks = {'status': 'configuration_error', 'errorCode': 'ROUTINE_STOCK_CONFIGURATION_INVALID', 'requests': 0}
        except JobLockedError:
            stocks = {'status': 'locked', 'errorCode': 'ROUTINE_STOCK_LOCKED', 'requests': 0}
        except Exception:
            stocks = {'status': 'failed', 'errorCode': 'ROUTINE_STOCKS_FAILED', 'requests': 0 if dry_run else None}
    if financial_selected:
        try:
            financial = paced(run_financial_step,settings,now=instant,dry_run=dry_run,publish_requested=publish)
        except Exception:
            financial = {'status':'failed','code':'ROUTINE_FINANCIAL_FAILED','requestCount':0 if dry_run else None}
    notices = {'status': 'not_selected'}
    if notices_selected:
        try:
            notices = run_announcements(settings, page_budget=notice_policy['maxPagesPerRun'],
                                        revisit_days=notice_policy['revisitDays'], dry_run=dry_run, publish=publish)
        except Exception:
            notices = {'status': 'failed', 'code': 'ROUTINE_ANNOUNCEMENT_FAILED', 'requestCount': 0 if dry_run else None}
    if forecast_selected:
        # 共用财务请求预算。主任务的实际请求数未知时，将剩余预算留到下次。
        used = financial.get('requestCount')
        budget = max(0, financial_policy['maxHttpPerRun'] - used) if isinstance(used, int) else 0
        try:
            forecast = paced(run_forecast_step, settings, now=instant, dry_run=dry_run, budget=budget, publish_requested=publish,
                policy={**forecast_config, 'timeoutSeconds': financial_policy['timeoutSeconds']})
        except Exception:
            forecast = {'status': 'failed', 'code': 'ROUTINE_FORECAST_FAILED', 'requestCount': 0 if dry_run else None}
    if valuation_selected:
        try:
            valuation = paced(run_valuation_step,settings,now=instant,dry_run=dry_run,publish_requested=publish)
        except Exception:
            valuation = {'status':'failed','code':'ROUTINE_VALUATION_FAILED','requestCount':0 if dry_run else None}
    if business_selected:
        try:
            business = paced(run_business_step,settings,now=instant,dry_run=dry_run,publish_requested=publish)
        except Exception:
            business = {'status':'failed','code':'ROUTINE_BUSINESS_FAILED','requestCount':0 if dry_run else None}
    if portfolio_selected:
        try:
            portfolio = paced(run_portfolio_step,settings,now=instant,dry_run=dry_run,publish=publish)
            if (not only_portfolio and publish and not dry_run
                    and portfolio.get('dividends', {}).get('publishReady')):
                from app.db.session import create_engine_and_session
                from app.stocks.marts.metric_cards import rebuild_metric_cards
                engine, factory = create_engine_and_session(settings.database_url)
                try:
                    portfolio['metricCards'] = rebuild_metric_cards(factory, dry_run=False, shareholder_returns=True)
                except Exception:
                    portfolio['metricCards'] = {'status': 'failed', 'code': 'WORKBENCH_CARD_PUBLICATION_FAILED'}
                else:
                    # 分红修订可能改变刚发布的工作台估算。组合须读到同一轮的新值。
                    if (portfolio['metricCards']['status'] == 'completed'
                            and portfolio.get('publication', {}).get('status') in {'completed', 'no_change'}):
                        from app.portfolio.marts.portfolio_runtime import rebuild_portfolio
                        try:
                            portfolio['publication'] = rebuild_portfolio(factory, dry_run=False)
                        except Exception:
                            portfolio['publication'] = {'status':'failed', 'code':'PORTFOLIO_YIELD_REFRESH_FAILED'}
                        if portfolio['publication']['status'] not in {'completed', 'no_change'}:
                            portfolio['status'] = 'partial'
                finally:
                    engine.dispose()
                if portfolio['metricCards']['status'] not in {'completed', 'no_change'}:
                    portfolio['status'] = 'partial'
        except Exception:
            portfolio = {'status':'failed','code':'ROUTINE_PORTFOLIO_FAILED','requestCount':0 if dry_run else None}
    if settings.routine_market_enabled:
        try:
            market = paced(run_market_step, settings, now=instant, dry_run=dry_run, publish=publish)
        except JobConfigurationError:
            market = {'status': 'configuration_error', 'code': 'ROUTINE_MARKET_CONFIGURATION_INVALID', 'requestCount': 0}
        except JobLockedError:
            market = {'status': 'locked', 'code': 'ROUTINE_MARKET_LOCKED', 'requestCount': 0}
        except Exception:
            market = {'status': 'failed', 'code': 'ROUTINE_MARKET_FAILED', 'requestCount': 0 if dry_run else None}
    if settings.routine_market_pulse_enabled:
        try:
            pulse = paced(run_pulse_step, settings, now=instant, dry_run=dry_run, publish=publish)
        except Exception:
            pulse = {'status': 'failed', 'code': 'ROUTINE_PULSE_FAILED', 'requestCount': 0 if dry_run else None}
    if settings.routine_region_enabled:
        try:
            region = paced(run_region_step, settings, now=instant, dry_run=dry_run, publish=publish)
        except Exception:
            region = {'status':'failed','code':'ROUTINE_REGION_FAILED','requestCount':0 if dry_run else None}
    if settings.routine_etf_heatmap_enabled:
        try:
            etf = paced(run_etf_step, settings, now=instant, dry_run=dry_run, publish=publish)
        except Exception:
            etf = {'status':'failed','code':'ROUTINE_ETF_FAILED','requestCount':0 if dry_run else None}
    if settings.routine_commodities_enabled:
        try:
            commodities = paced(run_commodity_step, settings, now=instant, dry_run=dry_run, publish=publish)
        except Exception:
            commodities = {'status':'failed','code':'ROUTINE_COMMODITIES_FAILED','requestCount':0 if dry_run else None}
    if extensions_selected:
        try:
            extensions = (run_macro_extensions(settings, now=instant, dry_run=True) if dry_run
                else run_macro_extensions(settings, before_send=pacer.before_send if pacer is not None else None))
        except Exception:
            extensions = {'status': 'failed', 'errorCode': 'ROUTINE_EXTENSIONS_FAILED',
                'supplierHttp': 0 if dry_run else None}
    if settings.routine_macro_enabled:
        try:
            if dry_run:
                macro = {**read_macro_catchup_plan(settings, now=instant), 'status': 'planned'}
            else:
                macro = paced(run_macro_catchup, settings, now=instant)
                if publish and _step_ready(macro, macro=True, publish=False):
                    try:
                        macro['publication'] = asdict(macro_rebuild(settings))
                    except JobLockedError:
                        macro['publication'] = {'status': 'locked', 'errorCode': 'ROUTINE_MACRO_PUBLICATION_LOCKED'}
                    except Exception:
                        macro['publication'] = {'status': 'failed', 'errorCode': 'ROUTINE_MACRO_PUBLICATION_FAILED'}
        except Exception:
            macro = {'status': 'failed', 'errorCode': 'ROUTINE_MACRO_FAILED', 'requestCount': 0 if dry_run else None}
    elif extensions_selected and publish and not dry_run:
        try:
            extensions['publication'] = asdict(macro_rebuild(settings))
        except JobLockedError:
            extensions['publication'] = {'status': 'locked', 'errorCode': 'ROUTINE_MACRO_PUBLICATION_LOCKED'}
        except Exception:
            extensions['publication'] = {'status': 'failed', 'errorCode': 'ROUTINE_MACRO_PUBLICATION_FAILED'}
    values = (stocks, market, pulse, region, etf, commodities, macro, extensions, portfolio, financial, forecast, valuation,business, notices)
    statuses = {v['status'] for v in values} | {_publication_status(v) for v in values}
    if 'locked' in statuses:
        status = 'locked'
    elif 'configuration_error' in statuses:
        status = 'configuration_error'
    elif 'failed' in statuses:
        status = 'failed'
    elif dry_run:
        status = 'planned'
    elif (_step_ready(stocks, macro=False, publish=publish) and _step_ready(macro, macro=True, publish=publish)
        and _market_ready(market, publish=publish) and _market_ready(pulse, publish=publish)
        and _market_ready(region, publish=publish) and _market_ready(etf, publish=publish) and _market_ready(commodities, publish=publish) and _market_ready(portfolio,publish=publish) and _market_ready(financial,publish=publish) and _market_ready(valuation,publish=publish) and _market_ready(business,publish=publish)
        and forecast['status'] in {'completed', 'not_selected'}
        and notices['status'] in {'completed', 'not_selected'}
        and (not extensions_selected or (extensions['status'] in {'completed', 'disabled'}
            and (not publish or settings.routine_macro_enabled or _publication_status(extensions) == 'completed')))):
        status = 'completed'
    else:
        status = 'partial'
    stock_http = stocks.get('requests', 0 if stocks['status'] == 'not_selected' else None)
    macro_http = macro.get('requestCount', 0 if macro['status'] == 'not_selected' else None)
    total_http = stock_http + macro_http if stock_http is not None and macro_http is not None else None
    counts = {'stocks': stock_http, 'macro': macro_http, 'total': total_http}
    if extensions_selected:
        counts['extensions'] = extensions.get('supplierHttp')
        counts['total'] = total_http + counts['extensions'] if total_http is not None and counts['extensions'] is not None else None
    if settings.routine_market_enabled:
        counts['market'] = market.get('requestCount')
        counts['total'] = counts['total'] + counts['market'] if counts['total'] is not None and counts['market'] is not None else None
    if settings.routine_market_pulse_enabled:
        counts['pulse'] = pulse.get('requestCount')
        counts['total'] = counts['total'] + counts['pulse'] if counts['total'] is not None and counts['pulse'] is not None else None
    if settings.routine_region_enabled:
        counts['region'] = region.get('requestCount')
        counts['total'] = counts['total'] + counts['region'] if counts['total'] is not None and counts['region'] is not None else None
    if settings.routine_etf_heatmap_enabled:
        counts['etf'] = etf.get('requestCount')
        counts['total'] = counts['total'] + counts['etf'] if counts['total'] is not None and counts['etf'] is not None else None
    if settings.routine_commodities_enabled:
        counts['commodities'] = commodities.get('requestCount')
        counts['total'] = counts['total'] + counts['commodities'] if counts['total'] is not None and counts['commodities'] is not None else None
    if portfolio_selected:
        counts['portfolio'] = portfolio.get('requestCount')
        counts['total'] = counts['total'] + counts['portfolio'] if counts['total'] is not None and counts['portfolio'] is not None else None
    if financial_selected:
        counts['financial'] = financial.get('requestCount')
        counts['total'] = counts['total'] + counts['financial'] if counts['total'] is not None and counts['financial'] is not None else None
    if forecast_selected:
        counts['forecastDetail'] = forecast.get('requestCount')
        counts['total'] = counts['total'] + counts['forecastDetail'] if counts['total'] is not None and counts['forecastDetail'] is not None else None
    if valuation_selected:
        counts['valuation'] = valuation.get('requestCount')
        counts['total'] = counts['total'] + counts['valuation'] if counts['total'] is not None and counts['valuation'] is not None else None
    if business_selected:
        counts['business'] = business.get('requestCount')
        counts['total'] = counts['total'] + counts['business'] if counts['total'] is not None and counts['business'] is not None else None
    if notices_selected:
        counts['announcements'] = notices.get('requestCount')
        counts['total'] = counts['total'] + counts['announcements'] if counts['total'] is not None and counts['announcements'] is not None else None
    return {'status': status, 'scope': 'development-active-only', 'startedAt': instant.isoformat(),
        'dryRun': dry_run, 'publishRequested': publish, 'stocks': stocks, 'market': market, 'macro': macro,
        'httpCounts': counts, **({'pulse': pulse} if settings.routine_market_pulse_enabled else {}),
        **({'extensions': extensions} if extensions_selected else {}),
        **({'region':region} if settings.routine_region_enabled else {}),
        **({'etf':etf} if settings.routine_etf_heatmap_enabled else {}),
        **({'commodities':commodities} if settings.routine_commodities_enabled else {}),
        **({'portfolio':portfolio} if portfolio_selected else {}),
        **({'financial':financial} if financial_selected else {}),
        **({'forecastDetail':forecast} if forecast_selected else {}),
        **({'announcements':notices} if notices_selected else {}),
        **({'valuation':valuation} if valuation_selected else {}),
        **({'business':business} if business_selected else {}),
        'publicationAtomicity': 'per-product-group', 'schedulerStarted': False}


def _market_ready(value: dict, *, publish: bool) -> bool:
    if value['status'] in {'not_selected','not_due'}:
        return True
    return value['status'] in {'completed','no_change'} and value.get('publishReady') is True and (
        not publish or _publication_status(value) in {'completed','no_change'})


def routine_exit_code(report: dict) -> int:
    return {'completed': 0, 'planned': 0, 'configuration_error': 2, 'locked': 5, 'failed': 3, 'partial': 4}[report['status']]
