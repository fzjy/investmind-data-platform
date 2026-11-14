"""扩展数据组命令：只读，无覆盖源/时钟或执行选项。"""
import json

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
import typer

from app.config import Settings
from app.jobs.run_errors import JobConfigurationError
from app.macro.extensions.config import ExtensionError


def register_macro_extension_commands(app: typer.Typer) -> None:
    @app.command('recover-tier1-housing')
    def recover_tier1_housing(failed_run_id: str = typer.Option(...,'--failed-run-id'),dry_run: bool = typer.Option(False,'--dry-run')) -> None:
        """本历史原网络失败的具名最多一次恢复；保留原父/根，共用原总预算。"""
        from app.macro.jobs.housing_history_runtime import run_housing_history
        try:result=run_housing_history(Settings.from_env(),dry_run=dry_run,recovery_parent_id=failed_run_id)
        except Exception:result=dict(status='blocked',readOnly=True,supplierHttp=0,errorCode='HISTORY_RECOVERY_CONFIGURATION_INVALID')
        typer.echo(json.dumps(result,ensure_ascii=True,allow_nan=False))
        if result['status'] in ('blocked','locked','replay_pending'):raise typer.Exit(2)

    @app.command('backfill-tier1-housing')
    def backfill_tier1_housing(dry_run: bool = typer.Option(False,'--dry-run')) -> None:
        """按集中配置续采一线新房/二手历史；无强制/时钟/源覆盖选项，不自动发布。"""
        from app.macro.jobs.housing_history_runtime import run_housing_history
        try:result=run_housing_history(Settings.from_env(),dry_run=dry_run)
        except Exception:result=dict(status='blocked',readOnly=True,supplierHttp=0,errorCode='HISTORY_CONFIGURATION_INVALID')
        typer.echo(json.dumps(result,ensure_ascii=True,allow_nan=False))
        if result['status'] in ('blocked','locked','replay_pending'):raise typer.Exit(2)

    @app.command('replay-official-access-check')
    def replay_official_access_check(failed_run_id: str = typer.Option(...,'--failed-run-id'),
        dry_run: bool = typer.Option(False,'--dry-run')) -> None:
        """零HTTP恢复已验证住户官方诊断原件，保留原父失败，不自动发布。"""
        from app.macro.jobs.official_access_replay import run_official_access_replay
        from app.macro.official_access_check import validate_parent_id
        try:
            validate_parent_id(failed_run_id)
            result=run_official_access_replay(Settings.from_env(),failed_run_id,dry_run=dry_run)
        except Exception:
            result=dict(status='blocked',readOnly=True,supplierHttp=0,publicationRequested=False,
                recoveryCompleted=False,errorCode='OFFICIAL_REPLAY_CONFIGURATION_INVALID')
        typer.echo(json.dumps(result,ensure_ascii=True,allow_nan=False))
        if result['status'] in ('blocked','locked'):raise typer.Exit(2)

    @app.command('check-official-macro-access')
    def check_official_macro_access(failed_run_id: str = typer.Option(...,'--failed-run-id'),
        dry_run: bool = typer.Option(False,'--dry-run')) -> None:
        """原住户403失败额外最多1次持久访问检查；不恢复原根、不入Core或发布。"""
        from app.macro.jobs.official_access_check import run_official_access_check
        from app.macro.official_access_check import validate_parent_id
        try:
            validate_parent_id(failed_run_id)
            result=run_official_access_check(Settings.from_env(),failed_run_id,dry_run=dry_run)
        except Exception:
            result=dict(status='blocked',readOnly=True,supplierHttp=0,publicationRequested=False,
                recoveryCompleted=False,errorCode='OFFICIAL_ACCESS_CONFIGURATION_INVALID')
        typer.echo(json.dumps(result,ensure_ascii=True,allow_nan=False))
        if result['status'] in ('blocked','locked','response_invalid'):raise typer.Exit(2)

    @app.command('replay-sz-market-calibration')
    def replay_sz_market_calibration(dry_run: bool = typer.Option(False, '--dry-run')) -> None:
        """零HTTP标定已存深市两A板块原件；不改原件或自动发布。"""
        from app.macro.jobs.sz_market_calibration_runtime import run_sz_market_calibration
        try:
            result = run_sz_market_calibration(Settings.from_env(), dry_run=dry_run)
        except (ValueError, SQLAlchemyError, OSError, KeyError, RuntimeError, TypeError):
            result = {'status': 'blocked', 'readOnly': dry_run, 'supplierHttp': 0,
                'errorCode': 'SZ_CALIBRATION_REPLAY_FAILED', 'publicationRequested': False}
        typer.echo(json.dumps(result, ensure_ascii=True, allow_nan=False))
        if result['status'] == 'blocked': raise typer.Exit(2)

    @app.command('sample-sz-market-contract')
    def sample_sz_market_contract(dry_run: bool = typer.Option(False, '--dry-run')) -> None:
        """一次性深市聚合原件采样；仅Raw/总1HTTP，不验证单位或发布。"""
        from app.macro.jobs.sz_market_sample_runtime import run_sz_market_sample
        try:
            result = run_sz_market_sample(Settings.from_env(), dry_run=dry_run)
        except (ValueError, SQLAlchemyError, OSError, KeyError, RuntimeError):
            result = {'status': 'blocked', 'readOnly': True, 'supplierHttp': 0,
                'errorCode': 'SZ_SAMPLE_CONFIGURATION_INVALID', 'publicationRequested': False}
        typer.echo(json.dumps(result, ensure_ascii=True, allow_nan=False))
        if result['status'] == 'blocked':
            raise typer.Exit(2)

    @app.command('replay-official-fiscal-raw')
    def replay_official_fiscal_raw(failed_run_id: str = typer.Option(...,'--failed-run-id'),
        dry_run: bool = typer.Option(False,'--dry-run')) -> None:
        """零HTTP重放原校验失败原件，保留原失败/预算；不发布。"""
        from app.macro.jobs.macro_fiscal_runtime import run_fiscal_raw_replay
        try:
            result = run_fiscal_raw_replay(Settings.from_env(),failed_run_id,dry_run=dry_run)
        except (JobConfigurationError,SQLAlchemyError,ValueError,OSError,KeyError,RuntimeError):
            result = {'status':'blocked','readOnly':True,'supplierHttp':0,'errorCode':'FISCAL_RAW_REPLAY_CONFIGURATION_INVALID'}
        typer.echo(json.dumps(result,ensure_ascii=True,allow_nan=False))
        if result['status'] in ('blocked','locked'):
            raise typer.Exit(2)

    @app.command('recover-macro-extensions')
    def recover_macro_extensions(
        failed_run_id: list[str] = typer.Option(..., '--failed-run-id'),
        dry_run: bool = typer.Option(False, '--dry-run'),
    ) -> None:
        """明确授权后，对每个原校验失败作业额外最多1请求；最多两组，不清旧记录。"""
        from app.macro.jobs.macro_extension_recovery import run_macro_extension_recovery
        try:
            result = run_macro_extension_recovery(Settings.from_env(), failed_run_id, dry_run=dry_run)
        except (JobConfigurationError, SQLAlchemyError, ValueError, OSError, KeyError, RuntimeError):
            typer.echo(json.dumps({'status': 'failed', 'errorCode': 'MACRO_EXTENSION_RECOVERY_FAILED'}))
            raise typer.Exit(2) from None
        typer.echo(json.dumps(result, ensure_ascii=True, allow_nan=False))
        if result['status'] in ('blocked', 'locked', 'replay_pending'):
            raise typer.Exit(2)

    @app.command('sync-macro-extensions')
    def sync_macro_extensions(dry_run: bool = typer.Option(False, '--dry-run'),
        group_id: list[str] | None = typer.Option(None, '--group-id')) -> None:
        """独立有界扩展采集/Raw重放；dry-run只读，无发布或强制选项。"""
        from app.macro.jobs.macro_extension_runtime import run_macro_extensions
        try:
            result = run_macro_extensions(Settings.from_env(), dry_run=dry_run, group_ids=group_id)
        except (JobConfigurationError, SQLAlchemyError, ValueError, OSError, KeyError, RuntimeError):
            typer.echo(json.dumps({'status': 'failed', 'errorCode': 'MACRO_EXTENSION_EXECUTION_FAILED'}))
            raise typer.Exit(2) from None
        typer.echo(json.dumps(result, ensure_ascii=True, allow_nan=False))
        if result['status'] in ('blocked', 'locked', 'replay_pending'):
            raise typer.Exit(2)

    @app.command('preview-macro-extensions')
    def preview_macro_extensions() -> None:
        """只读扩展查询计划和Core库存；不采集、预留、迁移或发布。"""
        from app.macro.extensions.preview import preview_macro_extensions as preview
        try:
            settings = Settings.from_env()
            if settings.data_backend != 'database' or not settings.database_url:
                typer.echo(json.dumps({'status': 'failed', 'errorCode': 'MACRO_EXTENSION_DATABASE_REQUIRED'}))
                raise typer.Exit(2)
            result = preview(settings)
        except ValidationError as error:
            missing = any(detail['loc'] == () and detail['type'] == 'value_error'
                and detail['msg'] == 'Value error, DANG_DATABASE_URL is required when DANG_DATA_BACKEND=database'
                for detail in error.errors(include_input=False, include_url=False, include_context=False))
            code = 'MACRO_EXTENSION_DATABASE_REQUIRED' if missing else 'MACRO_EXTENSION_PREVIEW_FAILED'
            typer.echo(json.dumps({'status': 'failed', 'errorCode': code}))
            raise typer.Exit(2) from None
        except (ExtensionError, JobConfigurationError, SQLAlchemyError, ValueError, OSError, KeyError):
            typer.echo(json.dumps({'status': 'failed', 'errorCode': 'MACRO_EXTENSION_PREVIEW_FAILED'}))
            raise typer.Exit(2) from None
        typer.echo(json.dumps(result, ensure_ascii=True, allow_nan=False))
