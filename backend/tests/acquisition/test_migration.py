from pathlib import Path
from alembic.config import Config
from alembic.script import ScriptDirectory

def test_acquisition_is_single_migration_head():
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root/'backend/alembic.ini'))
    assert ScriptDirectory.from_config(config).get_heads() == ['20261117_0023']
