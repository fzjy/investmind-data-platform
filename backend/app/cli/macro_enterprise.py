"""企业样本只读验算入口，不挂接任何采集或发布作业。"""
from datetime import datetime
from dataclasses import replace
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
import typer

from app.config import Settings
from app.jobs.run_errors import JobConfigurationError
from app.macro.enterprise import EnterprisePolicy, build_enterprise_analysis
from app.macro.enterprise_repository import load_enterprise_inputs, resolve_enterprise_selection
from app.shared.readonly_database import open_readonly_factory


def register_macro_enterprise_commands(app: typer.Typer) -> None:
    @app.command('sync-enterprise-target-evidence')
    def sync_enterprise_target_evidence(dry_run: bool = typer.Option(False, '--dry-run')) -> None:
        """单根有界指数/ETF候选采集；默认关闭，dry-run只读，不迁移或发布。"""
        from app.macro.jobs.target_execution_runtime import run_target_execution
        try:
            result = run_target_execution(Settings.from_env(), dry_run=dry_run)
            typer.echo(json.dumps(result, ensure_ascii=False, allow_nan=False))
        except (JobConfigurationError, SQLAlchemyError, ValueError, OSError, RuntimeError, ZoneInfoNotFoundError):
            typer.echo(json.dumps({'status': 'failed', 'errorCode': 'TARGET_EXECUTION_FAILED'}))
            raise typer.Exit(code=2) from None
        if result['status'] in ('blocked', 'failed', 'locked'):
            raise typer.Exit(code=2)

    @app.command('preview-enterprise-target-evidence')
    def preview_enterprise_target_evidence() -> None:
        """只读报告v2策略与候选证据状态；不采集、迁移或发布。"""
        from app.macro.target_evidence_preview import preview_target_evidence
        try:
            settings = Settings.from_env()
            if settings.data_backend != 'database' or not settings.database_url:
                typer.echo(json.dumps({'status': 'failed', 'errorCode': 'ENTERPRISE_DATABASE_REQUIRED'}))
                raise typer.Exit(code=2)
            result = preview_target_evidence(settings)
            typer.echo(json.dumps(result, ensure_ascii=False, allow_nan=False))
        except ValidationError as error:
            missing_database = any(
                detail['loc'] == () and detail['type'] == 'value_error'
                and detail['msg'] == 'Value error, DANG_DATABASE_URL is required when DANG_DATA_BACKEND=database'
                for detail in error.errors(include_input=False, include_url=False, include_context=False))
            code = 'ENTERPRISE_DATABASE_REQUIRED' if missing_database else 'TARGET_EVIDENCE_PREVIEW_FAILED'
            typer.echo(json.dumps({'status': 'failed', 'errorCode': code}))
            raise typer.Exit(code=2) from None
        except (JobConfigurationError, SQLAlchemyError, ValueError, OSError, ZoneInfoNotFoundError):
            typer.echo(json.dumps({'status': 'failed', 'errorCode': 'TARGET_EVIDENCE_PREVIEW_FAILED'}))
            raise typer.Exit(code=2) from None

    @app.command('preview-enterprise-targets')
    def preview_enterprise_targets() -> None:
        """只读报告指数/ETF目标依赖；不探测权限、采集、扩池或发布。"""
        from app.macro.enterprise_target_catalog import load_enterprise_target_catalog
        from app.macro.enterprise_target_preview import preview_enterprise_targets as build_preview
        from app.macro.enterprise_target_repository import load_target_evidence_inventory
        try:
            settings = Settings.from_env()
            if settings.data_backend != 'database' or not settings.database_url:
                typer.echo(json.dumps({'status': 'failed', 'errorCode': 'ENTERPRISE_DATABASE_REQUIRED'}))
                raise typer.Exit(code=2)
            catalog = load_enterprise_target_catalog()
            as_of = datetime.now(ZoneInfo(settings.sync_timezone)).date()
            with open_readonly_factory(settings.database_url) as factory:
                selection = resolve_enterprise_selection(factory, settings)
                inventory = load_target_evidence_inventory(factory)
                result = build_preview(selection, catalog, inventory, as_of)
            typer.echo(json.dumps(result, ensure_ascii=False, allow_nan=False))
        except ValidationError as error:
            # Settings的after验证器会先拒绝缺URL；只识别其精确安全错误，
            # 不另读环境/.env，也不把所有配置错误误归成缺数据库。
            missing_database = any(
                detail['loc'] == () and detail['type'] == 'value_error'
                and detail['msg'] == 'Value error, DANG_DATABASE_URL is required when DANG_DATA_BACKEND=database'
                for detail in error.errors(include_input=False, include_url=False, include_context=False)
            )
            code = 'ENTERPRISE_DATABASE_REQUIRED' if missing_database else 'ENTERPRISE_TARGET_PREVIEW_FAILED'
            typer.echo(json.dumps({'status': 'failed', 'errorCode': code}))
            raise typer.Exit(code=2) from None
        except (JobConfigurationError, SQLAlchemyError, ValueError, OSError, ZoneInfoNotFoundError):
            typer.echo(json.dumps({'status': 'failed', 'errorCode': 'ENTERPRISE_TARGET_PREVIEW_FAILED'}))
            raise typer.Exit(code=2) from None

    def run_rea_inputs(dry_run, *, probe=False, source=None):
        from app.macro.jobs.rea_inputs_runtime import run_enterprise_rea_inputs
        try:
            result=run_enterprise_rea_inputs(Settings.from_env(),dry_run=dry_run,probe=probe,source=source)
            typer.echo(json.dumps(result,ensure_ascii=False,allow_nan=False))
            if result['status']=='blocked':
                raise typer.Exit(code=2)
        except (JobConfigurationError,SQLAlchemyError,ValueError,OSError,ZoneInfoNotFoundError):
            typer.echo(json.dumps({'status':'failed','errorCode':'REA_INPUT_RUN_FAILED'}))
            raise typer.Exit(code=2) from None

    @app.command('sync-enterprise-rea-inputs')
    def sync_enterprise_rea_inputs(dry_run: bool=typer.Option(False,'--dry-run')) -> None:
        """手动有界执行REA多字段补充；不自动迁移或发布。"""
        run_rea_inputs(dry_run)

    @app.command('probe-enterprise-rea-inputs')
    def probe_enterprise_rea_inputs(dry_run: bool=typer.Option(False,'--dry-run'),
                                  source: str=typer.Option('balance','--source')) -> None:
        """对balance/cashflow之一最多一次新请求；缓存不作为本次权限证明。"""
        run_rea_inputs(dry_run,probe=True,source=source)

    @app.command('preview-enterprise-rea-inputs')
    def preview_enterprise_rea_inputs() -> None:
        """只读规划REA五节点必要字段；不验证权限、采集、迁移或发布。"""
        from app.macro.rea_input_preview import preview_rea_inputs
        try:
            settings=Settings.from_env()
            if settings.data_backend!='database' or not settings.database_url:
                typer.echo(json.dumps({'status':'failed','errorCode':'ENTERPRISE_DATABASE_REQUIRED'}))
                raise typer.Exit(code=2)
            policy=EnterprisePolicy(settings.macro_enterprise_min_coverage,
                settings.macro_enterprise_min_samples,settings.macro_enterprise_max_quarters)
            as_of=datetime.now(ZoneInfo(settings.sync_timezone)).date()
            with open_readonly_factory(settings.database_url) as factory:
                selection=resolve_enterprise_selection(factory,settings)
                inputs=load_enterprise_inputs(factory,selection)
                result=preview_rea_inputs(inputs,as_of,policy,settings.macro_enterprise_supplement_quarters,
                    settings.macro_catchup_request_limit)
            typer.echo(json.dumps(result,ensure_ascii=False,allow_nan=False))
        except (JobConfigurationError,SQLAlchemyError,ValueError,OSError,ZoneInfoNotFoundError):
            typer.echo(json.dumps({'status':'failed','errorCode':'ENTERPRISE_REA_INPUT_PREVIEW_FAILED'}))
            raise typer.Exit(code=2) from None

    def run_supplement(dry_run: bool, *, probe: bool = False, orders: bool = False) -> None:
        from app.macro.jobs.receivables_runtime import run_enterprise_receivables
        from app.macro.balance_supplement import ORDERS
        try:
            result = run_enterprise_receivables(Settings.from_env(), dry_run=dry_run, probe=probe,
                **({'target':ORDERS} if orders else {}))
            typer.echo(json.dumps(result, ensure_ascii=False, allow_nan=False))
            if result['status'] == 'blocked':
                raise typer.Exit(code=2)
        except (JobConfigurationError, SQLAlchemyError, ValueError, OSError, ZoneInfoNotFoundError):
            typer.echo(json.dumps({'status': 'failed', 'errorCode': 'ENTERPRISE_ORDER_RUN_FAILED' if orders else 'ENTERPRISE_RECEIVABLE_RUN_FAILED'}))
            raise typer.Exit(code=2) from None

    @app.command('probe-enterprise-orders')
    def probe_enterprise_orders(dry_run: bool = typer.Option(False, '--dry-run')) -> None:
        """最多1次物理尝试验证合同负债补充；缓存不算本次权限证明。"""
        run_supplement(dry_run,probe=True,orders=True)

    @app.command('sync-enterprise-orders')
    def sync_enterprise_orders(dry_run: bool = typer.Option(False, '--dry-run')) -> None:
        """手动有界补充合同负债；不自动迁移、发布或调度。"""
        run_supplement(dry_run,orders=True)

    @app.command('probe-enterprise-receivables')
    def probe_enterprise_receivables(dry_run: bool = typer.Option(False, '--dry-run')) -> None:
        """最多1次物理尝试验证应收补充；缓存不算本次权限证明。"""
        run_supplement(dry_run, probe=True)

    @app.command('sync-enterprise-receivables')
    def sync_enterprise_receivables(dry_run: bool = typer.Option(False, '--dry-run')) -> None:
        """手动分批补充应收；--dry-run强制只读，不自动迁移或发布。"""
        run_supplement(dry_run)

    @app.command('preview-enterprise-receivables')
    def preview_enterprise_receivables() -> None:
        """只读规划白条必要字段；不验证权限、不采集或写恢复游标。"""
        from app.macro.receivables_preview import preview_receivables
        try:
            settings=Settings.from_env()
            if settings.data_backend!='database' or not settings.database_url:
                typer.echo(json.dumps({'status':'failed','errorCode':'ENTERPRISE_DATABASE_REQUIRED'}))
                raise typer.Exit(code=2)
            policy=EnterprisePolicy(settings.macro_enterprise_min_coverage,
                settings.macro_enterprise_min_samples,settings.macro_enterprise_max_quarters)
            as_of=datetime.now(ZoneInfo(settings.sync_timezone)).date()
            with open_readonly_factory(settings.database_url) as factory:
                selection=resolve_enterprise_selection(factory,settings)
                data=load_enterprise_inputs(factory,selection)
                result=preview_receivables(data,as_of,policy,settings.macro_enterprise_supplement_quarters,
                    settings.macro_catchup_request_limit)
            typer.echo(json.dumps(result,ensure_ascii=False,allow_nan=False))
        except (JobConfigurationError,SQLAlchemyError,ValueError,OSError,ZoneInfoNotFoundError):
            typer.echo(json.dumps({'status':'failed','errorCode':'ENTERPRISE_PREVIEW_FAILED'}))
            raise typer.Exit(code=2) from None

    @app.command('audit-macro-enterprise')
    def audit_macro_enterprise() -> None:
        """只读验算配置范围的企业样本；成功不代表数据完整或已发布。"""
        try:
            settings=Settings.from_env()
            if settings.data_backend!='database' or not settings.database_url:
                typer.echo(json.dumps({'status':'failed','errorCode':'ENTERPRISE_DATABASE_REQUIRED'}))
                raise typer.Exit(code=2)
            policy=EnterprisePolicy(settings.macro_enterprise_min_coverage,
                settings.macro_enterprise_min_samples,settings.macro_enterprise_max_quarters)
            as_of=datetime.now(ZoneInfo(settings.sync_timezone)).date()
            with open_readonly_factory(settings.database_url) as factory:
                selection=resolve_enterprise_selection(factory,settings)
                inputs=load_enterprise_inputs(factory,selection)
                from app.macro.enterprise_receivables import load_enterprise_receivables
                inputs=load_enterprise_receivables(factory,inputs,as_of,policy,
                    page_size=settings.tushare_page_size,timezone=settings.sync_timezone)
                inputs=replace(inputs,cashflow_enabled=True,distribution_enabled=True)
                from app.macro.enterprise_repository import load_enterprise_loss_inputs
                inputs=replace(load_enterprise_loss_inputs(factory,inputs,as_of,policy),ledger_enabled=True)
                from app.macro.balance_supplement import ORDERS
                inputs=load_enterprise_receivables(factory,inputs,as_of,policy,
                    page_size=settings.tushare_page_size,timezone=settings.sync_timezone,target=ORDERS)
                inputs=replace(inputs,orders_enabled=True,renewal_estimation_enabled=True)
                from app.macro.enterprise_rea_inputs import load_enterprise_rea_inputs
                inputs=load_enterprise_rea_inputs(factory,inputs,as_of,policy,
                    page_size=settings.tushare_page_size,timezone=settings.sync_timezone)
                result=build_enterprise_analysis(inputs,as_of,policy)
                from app.macro.enterprise_rea import build_rea_projection
                result['rea']=build_rea_projection(inputs,as_of,policy,result)
                from app.macro.enterprise_contract import public_enterprise_analysis
                result['industryComparison']=public_enterprise_analysis(inputs,as_of,policy,industry_enabled=True)['industryComparison']
            typer.echo(json.dumps({'status':'completed','readOnly':True,**result},ensure_ascii=False,allow_nan=False))
        except (JobConfigurationError,SQLAlchemyError,ValueError,OSError,ZoneInfoNotFoundError):
            # 不回显数据库异常或配置输入，防止DSN/密钥进入输出。
            typer.echo(json.dumps({'status':'failed','errorCode':'ENTERPRISE_AUDIT_FAILED'}))
            raise typer.Exit(code=2) from None
