import json

import typer

from app.config import Settings


def register_official_member_commands(app):
    @app.command('recover-enterprise-issuer-samples')
    def recover_enterprise_issuer_samples(dry_run:bool=typer.Option(False,'--dry-run')):
        """保留旧失败父，以独立固定槽获取已登记恢复原件，不解析或发布。"""
        from app.macro.jobs.issuer_sample_runtime import run_issuer_samples
        try:result=run_issuer_samples(Settings.from_env(),dry_run=dry_run,recovery=True)
        except Exception:result={'status':'blocked','readOnly':True,'supplierHttp':0,'proofReady':False,'errorCode':'ISSUER_RECOVERY_CONFIGURATION_INVALID'}
        typer.echo(json.dumps(result,ensure_ascii=True,allow_nan=False))
        if result['status'] in ('blocked','locked'):raise typer.Exit(2)

    @app.command('capture-enterprise-issuer-samples')
    def capture_enterprise_issuer_samples(dry_run:bool=typer.Option(False,'--dry-run')):
        """先完成已登记成员图，再共享原额度保存发行人原件；不解析/发布。"""
        from app.macro.jobs.issuer_sample_runtime import run_issuer_samples
        try:result=run_issuer_samples(Settings.from_env(),dry_run=dry_run)
        except Exception:result={'status':'blocked','readOnly':True,'supplierHttp':0,'proofReady':False,'errorCode':'ISSUER_SAMPLE_CONFIGURATION_INVALID'}
        typer.echo(json.dumps(result,ensure_ascii=True,allow_nan=False))
        if result['status'] in ('blocked','locked'):raise typer.Exit(2)

    @app.command('sync-enterprise-fund-members')
    def sync_enterprise_fund_members(dry_run:bool=typer.Option(False,'--dry-run')):
        """复用已证声明，CSI实际日期驱动同日PCF；共享限额，不自动发布。"""
        from app.macro.jobs.fund_member_graph_runtime import run_fund_graphs
        try:result=run_fund_graphs(Settings.from_env(),dry_run=dry_run)
        except Exception:result={'status':'blocked','readOnly':True,'supplierHttp':0,'errorCode':'FUND_MEMBER_GRAPH_CONFIGURATION_INVALID'}
        typer.echo(json.dumps(result,ensure_ascii=True,allow_nan=False))
        if result['status'] in ('blocked','locked','replay_pending'):raise typer.Exit(2)

    @app.command('discover-enterprise-fund-contracts')
    def discover_enterprise_fund_contracts(dry_run:bool=typer.Option(False,'--dry-run')):
        """共用官方限额核查基金下载合同；不证明完整成员、不发布。"""
        from app.macro.jobs.fund_contract_runtime import run_fund_contracts
        try:result=run_fund_contracts(Settings.from_env(),dry_run=dry_run)
        except Exception:result={'status':'blocked','readOnly':True,'supplierHttp':0,'proofReady':False,'errorCode':'FUND_CONTRACT_CONFIGURATION_INVALID'}
        typer.echo(json.dumps(result,ensure_ascii=True,allow_nan=False))
        if result['status'] in ('blocked','locked','replay_pending'):raise typer.Exit(2)

    @app.command('sync-enterprise-catalog-members')
    def sync_enterprise_catalog_members(dry_run:bool=typer.Option(False,'--dry-run')):
        """剩余目录按目标冻结，复用官方日预算；dry-run只读，不发布。"""
        from app.macro.jobs.catalog_member_runtime import run_catalog_members
        try:result=run_catalog_members(Settings.from_env(),dry_run=dry_run)
        except Exception:
            result={'status':'blocked','readOnly':True,'supplierHttp':0,'publicationRequested':False,'errorCode':'CATALOG_MEMBER_CONFIGURATION_INVALID'}
        typer.echo(json.dumps(result,ensure_ascii=True,allow_nan=False))
        if result['status'] in ('blocked','locked','replay_pending'):raise typer.Exit(2)

    @app.command('sync-enterprise-official-members')
    def sync_enterprise_official_members(dry_run:bool=typer.Option(False,'--dry-run')):
        """独立官方原件预算与零HTTP重放；不采财报、不发布。"""
        from app.macro.jobs.official_member_runtime import run_official_members
        try:result=run_official_members(Settings.from_env(),dry_run=dry_run)
        except Exception:
            result={'status':'blocked','readOnly':True,'supplierHttp':0,'publicationRequested':False,'errorCode':'OFFICIAL_MEMBER_CONFIGURATION_INVALID'}
        typer.echo(json.dumps(result,ensure_ascii=True,allow_nan=False))
        if result['status'] in ('blocked','locked','replay_pending'):raise typer.Exit(2)
