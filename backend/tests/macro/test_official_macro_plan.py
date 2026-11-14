from copy import deepcopy
from datetime import datetime, timezone
from importlib import import_module

import pytest

from tests.macro.test_official_macro_config import policy


NOW = datetime(2026,10,25,6,tzinfo=timezone.utc)


@pytest.mark.parametrize('group', ['householdCredit','tier1Housing','specialBonds','propertyLoans'])
def test_official_plan_is_reconstructible_and_enabled_toggle_not_identity(group):
    module=import_module('app.macro.official_macro_plan')
    first=module.build_official_macro_plan(group,policy(group),NOW)
    value=policy(group);value['collectionEnabled']=True
    assert first == module.build_official_macro_plan(group,value,NOW)
    assert first['scheduledAt']=='2026-10-19T09:00:00+08:00'
    assert first['startPeriod']=='2022-10-01' and first['endPeriod']=='2026-09-01'
    assert first['latestQuarter']=='2026-Q3'
    assert len(first['documents'])==first['limits']['maxHttpPerPeriod']
    import_module('app.macro.extensions.plan').validate_extension_plan(first)


def test_tampered_plan_cannot_be_authorized_by_rehashing():
    module=import_module('app.macro.official_macro_plan')
    value=module.build_official_macro_plan('householdCredit',policy(),NOW)
    value['documents'][0]['url']='https://example.org/'
    value['fingerprint']=import_module('app.macro.extensions.plan').digest({k:v for k,v in value.items() if k!='fingerprint'})
    with pytest.raises(ValueError):
        import_module('app.macro.extensions.plan').validate_extension_plan(value)


def test_future_document_cannot_enter_a_historical_scheduled_window():
    module=import_module('app.macro.official_macro_plan')
    with pytest.raises(ValueError):
        module.build_official_macro_plan('householdCredit',policy(),datetime(2026,8,10,tzinfo=timezone.utc))


def test_extension_build_dispatch_keeps_its_closed_official_plan():
    config=import_module('app.macro.extensions.config').load_extension_config().model_dump()
    config['groups']['tier1Housing']=policy('tier1Housing')
    checked=import_module('app.macro.extensions.config').ExtensionConfig.model_validate(config)
    got=import_module('app.macro.extensions.plan').build_extension_plan('tier1Housing',checked,NOW)
    assert got==import_module('app.macro.official_macro_plan').build_official_macro_plan('tier1Housing',policy('tier1Housing'),NOW)
