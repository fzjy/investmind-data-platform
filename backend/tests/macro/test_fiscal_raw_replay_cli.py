import json

from typer.testing import CliRunner
from app.cli.main import app


def test_raw_replay_invalid_id_rejected_before_any_database(monkeypatch):
    import app.db.session as module
    monkeypatch.setattr(module,'create_engine_and_session',lambda *_: (_ for _ in ()).throw(AssertionError('must not open DB')))
    result=CliRunner().invoke(app,['replay-official-fiscal-raw','--failed-run-id','not-a-uuid','--dry-run'])
    assert result.exit_code==2
    value=json.loads(next(line for line in result.stdout.splitlines() if line.startswith('{')))
    assert value['errorCode']=='FISCAL_RAW_REPLAY_ID_INVALID' and value['supplierHttp']==0 and value['readOnly']
