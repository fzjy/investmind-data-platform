from contextlib import contextmanager

from app.config import Settings
from app.macro.extensions.config import ExtensionConfig, load_extension_config
from app.macro.extensions.preview import preview_macro_extensions
from backend.tests.macro.test_extension_store import database
from backend.tests.macro.test_official_macro_config import policy
from backend.tests.macro.test_official_macro_plan import NOW


def test_new_groups_preview_reads_ledger_without_reservation_or_http(database,monkeypatch):
    file,factory=database
    value=load_extension_config().model_dump()
    value['groups']['householdCredit']=policy()
    config=ExtensionConfig.model_validate(value)
    import app.macro.extensions.preview as preview
    monkeypatch.setattr(preview,'load_extension_config',lambda:config)
    @contextmanager
    def readonly(_):yield factory
    monkeypatch.setattr(preview,'open_readonly_factory',readonly)
    before=file.read_bytes()
    result=preview_macro_extensions(Settings(data_backend='database',database_url='sqlite:///not-opened'),now=NOW,group_ids=['householdCredit'])
    group=result['groups'][0]
    assert result['readOnly'] and result['supplierHttp']==0 and not result['planPersisted']
    assert group['execution']['status']=='new' and group['inventory']['validVisibleRows']==0
    assert group['inventory']['scope']=='frozen-plan-core-bindings'
    assert file.read_bytes()==before
