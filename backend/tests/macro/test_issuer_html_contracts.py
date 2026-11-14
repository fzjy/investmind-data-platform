"""发行人实际字段的缩小结构样本；不把比较基准或不完整网页当成员证明。"""
from hashlib import sha256
from importlib import import_module

import pytest


def api():return import_module('app.macro.issuer_html_contracts')


def bank():
    return '''<input id="fundCode" value="512800"><input id="shortName" value="银行ETF">
    <div id="fundDetailsList1"><td id="tem_name">华宝中证银行交易型开放式指数证券投资基金</td>
    <td id="run_way">512800</td><p id="floating_income_rate">中证银行指数</p>
    <p id="invest_scope">本基金的标的指数为中证银行指数。</p></div>
    <div id="other">经理同时管理中证医药指数基金</div>'''.encode()


def consumer():
    return '''<input id="fundid" value="159928"><h1 class="H1Pro">汇添富中证主要消费ETF
    <span class="fundNum">( 基金代码 159928 )</span></h1>
    <div id="rigInfo1"><p><strong>基准指数代码：</strong><span>000932.SH</span></p>
    <p><strong>基准指数简称：</strong><span>中证主要消费指数</span></p></div>'''.encode('gb18030')


def pcf():
    return '''<input id="fundid" value="159928"><h1 class="H1Pro">汇添富中证主要消费ETF
    <span class="fundNum">( 基金代码 159928 )</span></h1><input id="totalPages" value="1">
    <div id="etfdata"><input id="enddate" value="2026-10-28"><table class="infotable">
    <tr><th>最新公告日期</th><td>2026-10-28</td></tr>
    <tr><th>基金名称</th><td>消费ETF汇添富</td></tr>
    <tr><th>基金管理公司名称</th><td>汇添富基金管理股份有限公司</td></tr>
    <tr><th>证券代码</th><td>159928</td></tr>
    <tr><th>全部申购赎回组合证券只数</th><td>2 只</td></tr></table></div>
    <div id="mfnd2"><table class="sharetable intervalbg"><tr><th>证券代码</th><th>证券简称</th>
    <th>股票数量</th><th>现金替代标志</th><th>现金替代溢价比例</th><th>折价比例</th>
    <th>申购代替金额</th><th>赎回代替金额</th><th>挂牌市场</th></tr>
    <tr><td>000568</td><td>泸州老窖</td><td>1,200</td><td>允许</td><td>35.00%</td>
    <td></td><td></td><td></td><td>深圳证券交易所</td></tr>
    <tr><td>600519</td><td>贵州茅台</td><td>0</td><td>必须</td><td>10.00%</td>
    <td>30.00%</td><td></td><td></td><td>上海证券交易所</td></tr></table></div>'''


def test_bank_product_scoped_tracking_declaration_without_guessing_index_code():
    body=bank();v=api().parse_issuer_html_metadata(body,'512800')
    assert v['fundCode']=='512800' and v['fundName']=='华宝中证银行交易型开放式指数证券投资基金'
    assert v['trackingIndexName']=='中证银行指数' and v['trackingIndexCode'] is None
    assert v['rawSha256']==sha256(body).hexdigest() and v['membershipProven'] is False


def test_consumer_wrong_charset_meta_does_not_turn_benchmark_into_tracking_proof():
    body=b'<meta charset="UTF-8">'+consumer();v=api().parse_issuer_html_metadata(body,'159928')
    assert v['encoding']=='gb18030' and v['benchmarkIndexCode']=='000932.SH'
    assert v['benchmarkIndexName']=='中证主要消费指数'
    assert v['trackingIndexName'] is None and v['trackingIndexCode'] is None


@pytest.mark.parametrize('body',[
    bank().replace(b'value="512800"',b'value="512810"'),
    bank()+b'<input id="fundCode" value="512800">',
    bank().replace('本基金的标的指数为中证银行指数。'.encode(),'比较基准为中证银行指数。'.encode()),
    bank().replace('中证银行指数</p>'.encode(),'另一个指数</p>'.encode(),1),
    b'<script>'+bank()+b'</script>',b'<html>fundCode=512800</html>',
])
def test_ambiguous_wrong_or_template_identity_is_rejected(body):
    with pytest.raises(ValueError,match='ISSUER_HTML_METADATA_INVALID'):api().parse_issuer_html_metadata(body,'512800')


def test_consumer_complete_pcf_checks_real_day_declared_total_and_exchange():
    body=pcf().encode('gb18030');v=api().parse_consumer_pcf(body,'159928','2026-10-28')
    assert v['memberCodes']==['000568.SZ','600519.SH'] and v['memberCount']==2
    assert v['asOfDate']=='2026-10-28' and v['rawSha256']==sha256(body).hexdigest()
    assert v['membershipBasis']=='published-pcf-all-components-not-fund-holdings'


def test_actual_layout_has_separate_basic_pretrade_and_current_trade_tables():
    html=pcf().replace('<tr><th>证券代码</th>','</table><table class="infotable"><tr><th>证券代码</th>',1)
    assert api().parse_consumer_pcf(html.encode('gb18030'),'159928','2026-10-28')['memberCount']==2


def test_explicit_pcf_cash_record_counts_towards_completeness_but_is_not_a_stock():
    html=pcf().replace('600519</td><td>贵州茅台','159900</td><td>申赎现金').replace('上海证券交易所','深圳证券交易所')
    v=api().parse_consumer_pcf(html.encode('gb18030'),'159928','2026-10-28')
    assert v['componentRecordCount']==2 and v['memberCount']==1 and v['memberCodes']==['000568.SZ']
    assert v['excludedCashComponents']==['159900.SZ']


def test_cash_named_other_code_is_not_silently_excluded_or_returned_as_stock():
    html=pcf().replace('600519</td><td>贵州茅台','159901</td><td>申赎现金')
    with pytest.raises(ValueError):api().parse_consumer_pcf(html.encode('gb18030'),'159928','2026-10-28')


@pytest.mark.parametrize('old,new',[
    ('value="1"','value="2"'),('2 只','3 只'),('2 只','1 只'),
    ('最新公告日期</th><td>2026-10-28','最新公告日期</th><td>2026-10-27'),
    ('value="2026-10-28"','value="2026-10-27"'),
    ('600519','000568'),('上海证券交易所','未知市场'),
    ('1,200','1,20'),('1,200','NaN'),('股票数量','持仓权重'),
    ('value="159928"','value="159929"'),('汇添富基金管理股份有限公司','其他公司'),
    ('证券代码</th><td>159928','证券代码</th><td>159929'),
])
def test_consumer_truncated_paged_ambiguous_or_wrong_pcf_rejected(old,new):
    body=pcf().replace(old,new).encode('gb18030')
    with pytest.raises(ValueError,match='ISSUER_CONSUMER_PCF_INVALID'):api().parse_consumer_pcf(body,'159928','2026-10-28')


def test_pcf_maximum_is_dynamic_and_no_claim_of_index_equality_or_pool_intersection():
    with pytest.raises(ValueError):api().parse_consumer_pcf(pcf().encode('gb18030'),'159928','2026-10-28',max_members=1)
    v=api().parse_consumer_pcf(pcf().encode(),'159928','2026-10-28',max_members=3)
    assert 'poolMembers' not in v and 'proofReady' not in v and 'indexCode' not in v


@pytest.mark.parametrize('body',[b'',b'\xff',b'x'*8388609,b'<div>'*130+b'x'+b'</div>'*130],ids=['empty','invalid-encoding','too-large','too-deep'])
def test_body_and_tree_resource_bounds(body):
    with pytest.raises(ValueError):api().parse_issuer_html_metadata(body,'512800')
