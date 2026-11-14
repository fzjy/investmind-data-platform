from copy import deepcopy

from app.macro.extensions.view import _empty,build_extension_view,extension_view_valid
from backend.tests.e2e.export_controlled_exchange_summary import controlled


def test_standalone_exchange_view_needs_neither_fiscal_nor_official_housing():
    market=controlled();inputs={'sources':{g:_empty(g) for g in ('usdcnh','nominalGdp','vix')},'marketSummary':market}
    value=build_extension_view(inputs)
    assert value['schemaVersion']=='1.4' and value['fiscal'] is None and value['official'] is None
    assert value['marketSummary']==market and extension_view_valid(value)
    value['marketSummary']['monthly'][-1]['value']='100'
    assert not extension_view_valid(value)


def test_old_contract_without_exchange_keeps_exact_previous_shape():
    inputs={'sources':{g:_empty(g) for g in ('usdcnh','nominalGdp','vix')}}
    value=build_extension_view(inputs)
    assert value['schemaVersion']=='1.1' and 'marketSummary' not in value
    assert extension_view_valid(value)
