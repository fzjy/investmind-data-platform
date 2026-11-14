from importlib import import_module

import pytest


def body(code='512880',name='中证全指证券公司指数'):
    return f'<html><h3>证券ETF国泰<span>({code})</span></h3><p>本基金的标的指数为{name}及其未来可能发生的变更。</p></html>'.encode()


def parse(value=None):return import_module('app.macro.official_members').parse_fund_mapping(body() if value is None else value,'512880')


def test_explicit_product_identity_and_tracking_statement_are_required():
    value=parse();assert value['fundCode']=='512880' and value['trackingIndexName']=='中证全指证券公司指数'
    assert value['fundName']=='证券ETF国泰' and len(value['rawSha256'])==64
    assert 'indexCode' not in value  # 名称证据不冒充数字代码映射或完整持仓。


@pytest.mark.parametrize('bad',[
    body('512800'),b'<html>512880</html>',body().replace(b'<h3>',b'<div>').replace(b'</h3>',b'</div>'),
    '<h3>证券ETF国泰(512880)</h3><p>业绩比较基准中证全指证券公司指数收益率</p>'.encode(),
    b'<script>'+body()+b'</script>',
    body()+body(name='中证银行指数'),
    body().replace('指数及其'.encode(),'资产及其'.encode()),b'\xff',
])
def test_navigation_scripts_conflicting_identity_and_implicit_benchmark_do_not_prove_mapping(bad):
    with pytest.raises(ValueError):parse(bad)
