"""受控 HTML 口径反例；不调用供应商。"""
from importlib import import_module

import pytest


def module():
    return import_module('app.macro.official_macro_normalize')


def quota(value='44,000.00', label='2026年地方政府专项债务新增限额', unit='亿元'):
    return f'<h1>2025年地方政府专项债务余额情况表</h1><table><tr><td colspan="2">单位：{unit}</td></tr><tr><td>2025年末地方政府专项债务余额</td><td>1</td></tr><tr><td>{label}</td><td>{value}</td></tr><tr><td>2026年地方政府专项债务余额限额</td><td>443185.08</td></tr></table>'.encode()


def monthly(month=8, ytd='29293', direct='5213'):
    return f'<h1>2026年{month}月地方政府债券发行和债务余额情况</h1><p>2026年{month}月，全国发行新增债券6000亿元，其中一般债券787亿元、专项债券{direct}亿元。发行再融资专项债券999亿元。</p><p>2026年1—{month}月，全国发行新增债券36000亿元，其中一般债券6707亿元、专项债券{ytd}亿元。</p>'.encode()


def briefing():
    return '<h1>2026年前三季度财政收支情况新闻发布会</h1><p>1—9月，发行新增地方政府专项债券35608亿元，完成全年新增专项债务限额的81%。9月发行新增地方政府专项债券6339亿元。</p><p>从结存限额中安排5500亿元，其中一般债务3000亿元，专项债务2500亿元。</p>'.encode()


def test_quota_uses_semantic_new_special_row_not_prior_title_year_or_balance_limit():
    result = module().parse_special_quota(quota(), 2026)
    assert result['role'] == 'special-quota' and result['source'] == 'mof'
    assert result['period'] == '2026-01-01'
    assert result['points'][0]['value'] == '44000'
    assert result['points'][0]['seriesId'] == 'cn.fiscal.special_bonds_new_quota'
    assert result['points'][0]['frequency'] == 'annual'


@pytest.mark.parametrize('body', [
    quota(label='2026年地方政府专项债务余额限额'), quota(label='2025年地方政府专项债务新增限额'),
    quota(unit='万亿元'), quota(value='1<br>2'), quota(value='NaN'), quota(value='-1'),
    quota(value='44,00'), quota()+quota(), quota().replace(b'<td>44,000.00', b'<td colspan="2">44,000.00'),
    '<h1>quota</h1><p>单位：亿元</p>'.encode()+quota(unit='万亿元'),
])
def test_quota_wrong_semantics_units_bad_or_ambiguous_number_rejected(body):
    with pytest.raises(ValueError):
        module().parse_special_quota(body, 2026)


def test_monthly_new_special_values_exclude_general_and_refinancing():
    result = module().parse_special_issuance(monthly(), '2026-08', 'special-issuance')
    points = {point['seriesId']: point for point in result['points']}
    assert points['cn.fiscal.special_bonds_new_month']['value'] == '5213'
    assert points['cn.fiscal.special_bonds_new_ytd']['value'] == '29293'
    assert all(p['period'] == '2026-08-01' and p['unit'] == '亿元' for p in points.values())


def test_saved_mof_monthly_structure_allows_full_new_local_government_name_and_empty_heading():
    body=monthly().replace('新增债券'.encode(), '新增地方政府债券'.encode())+b'<h2></h2>'
    result=module().parse_special_issuance(body,'2026-08','special-issuance')
    assert [p['value'] for p in result['points']]==['5213','29293']


def test_briefing_keeps_reported_progress_direct_month_and_carryover_separate():
    result = module().parse_special_issuance(briefing(), '2026-09', 'special-briefing')
    points = {point['seriesId']: point for point in result['points']}
    assert points['cn.fiscal.special_bonds_new_month']['value'] == '6339'
    assert points['cn.fiscal.special_bonds_new_ytd']['value'] == '35608'
    assert points['cn.fiscal.special_bonds_reported_progress']['value'] == '81'
    assert points['cn.fiscal.special_bonds_reported_progress']['unit'] == '%'
    assert len(result['events']) == 1
    assert result['events'][0] == {'kind': 'special-carryover-allocation', 'amount': '2500', 'unit': '亿元', 'effectivePeriod': None}


def actual_briefing():
    return '<h1>2026年前三季度财政收支情况新闻发布会文字实录</h1><h2></h2><p>一是新增债券发行稳步提速。1—9月，全国共发行新增专项债券35608亿元，发行进度81%，其中，9月份发行6339亿元，较去年同期增加2148亿元。</p><p>1-9月，发行用于置换存量隐性债务的专项债券18977亿元，完成全年限额2万亿元的95%。</p><p>5500亿元地方政府债务结存限额中，安排使用3000亿元一般债务结存限额；安排使用2500亿元专项债务结存限额，分配给今年第四季度有实际项目资金需求的地区。</p>'.encode()


def test_saved_briefing_full_transcript_contextual_month_not_other_debt_progress():
    result=module().parse_special_issuance(actual_briefing(),'2026-09','special-briefing')
    assert [p['value'] for p in result['points']]==['6339','35608','81']
    assert result['events'][0]['amount']=='2500'


def test_contextual_month_outside_new_special_paragraph_is_not_accepted():
    body=actual_briefing().replace('其中，9月份发行6339亿元'.encode(),'无月度直报'.encode())
    body+='<p>9月份发行6339亿元。</p>'.encode()
    with pytest.raises(ValueError):module().parse_special_issuance(body,'2026-09','special-briefing')


@pytest.mark.parametrize('body,period,role', [
    (monthly(), '2026-09', 'special-issuance'),
    (monthly().replace('新增'.encode(), '再融资'.encode()), '2026-08', 'special-issuance'),
    (monthly().replace('专项债券'.encode(), '一般债券'.encode()), '2026-08', 'special-issuance'),
    (monthly(direct='-1'), '2026-08', 'special-issuance'),
    (monthly(direct='1<br>2'), '2026-08', 'special-issuance'),
    (monthly()+monthly(), '2026-08', 'special-issuance'),
    (briefing().replace('81%'.encode(), '81'.encode()), '2026-09', 'special-briefing'),
    (briefing(), '2026-08', 'special-briefing'),
    (briefing(), '2026-09', 'household-ytd'),
])
def test_invalid_special_document_never_substitutes_other_amounts(body, period, role):
    with pytest.raises(ValueError):
        module().parse_special_issuance(body, period, role)


def test_real_zero_is_not_missing():
    result = module().parse_special_issuance(monthly(ytd='0', direct='0'), '2026-08', 'special-issuance')
    assert [p['value'] for p in result['points']] == ['0', '0']


def test_vintage_conflict_preserves_direct_values_and_disables_cross_version_difference():
    result = module().derive_special_progress('44000', '35608', '6339', '29293', '81')
    assert result['quota'] == '44000' and result['ytd'] == '35608' and result['directMonth'] == '6339'
    assert result['reportedProgress'] == '81'
    assert result['crossVintageDifference'] == '6315' and result['differenceDiscrepancy'] == '24'
    assert result['derivedMonth'] is None and result['revisionConflict'] is True
    assert float(result['computedProgress']) == pytest.approx(80.92727272727272)


def test_progress_does_not_clamp_raw_over_100_or_require_prior_vintage():
    result = module().derive_special_progress('100', '120', '20', None, None)
    assert result['computedProgress'] == '120' and result['crossVintageDifference'] is None
    assert result['derivedMonth'] is None and result['revisionConflict'] is False


@pytest.mark.parametrize('quota,ytd,direct,prior,reported', [
    ('0','1','1',None,None), ('-1','1','1',None,None),
    ('100','NaN','1',None,None), ('100','1','-1',None,None),
    ('100','1','1',True,None), ('100','1','1',None,'81%'),
])
def test_progress_strict_units_and_nonnegative_source_amounts(quota,ytd,direct,prior,reported):
    with pytest.raises(ValueError):
        module().derive_special_progress(quota,ytd,direct,prior,reported)
