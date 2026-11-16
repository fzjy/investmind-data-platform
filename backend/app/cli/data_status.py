from datetime import UTC, datetime
import json
import typer

from app.acquisition.initialization import apply_initialization, plan_initialization
from app.acquisition.service import ROOT
from app.config import Settings
from app.db.session import create_engine_and_session


def register_data_status(app):
    @app.command('reconcile-data-status', help='按页核对已有采集凭证，默认仅输出报告。')
    def reconcile(limit:int=typer.Option(100,min=1,max=1000),cursor:str|None=None,apply:bool=False):
        from sqlalchemy import inspect
        engine,factory = create_engine_and_session(Settings.from_env().database_url)
        now = datetime.now(UTC)
        try:
            if apply and not inspect(engine).has_table('data_acquisition_state',schema='ops'):
                raise typer.BadParameter('请先迁移至 20261116_0022')
            page = plan_initialization(factory,root=ROOT,now=now,limit=limit,cursor=cursor)
            written = 0
            if apply:
                with factory.begin() as session:
                    written = apply_initialization(session,page.records)['written']
            typer.echo(json.dumps({'readAt':now.isoformat(),'dryRun':not apply,'scanned':page.scanned,
                                   'verified':len(page.records),'written':written,'nextCursor':page.next_cursor}))
        finally:
            engine.dispose()
