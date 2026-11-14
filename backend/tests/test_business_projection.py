from app.stocks.business_projection import project_business


def row(name='甲',sales=100,*,kind='P',currency='CNY',period='20251231',flag='0'):
    return dict(ts_code='600519.SH',end_date=period,bz_item=name,bz_sales=sales,bz_profit=20,bz_cost=80,curr_type=currency,update_flag=flag,businessType=kind)


def project(rows):
    return project_business(dict(rows=rows,queryComplete=True,sourceAsOf='2026-10-21',capturedAt='2026-11-06T00:00:00+00:00',sourceBaseVersion='base'))


def test_products_regions_and_currencies_have_independent_denominators():
    result=project([row('甲',100),row('乙',300),row('甲',999,kind='D'),row('乙',50,currency='USD')])
    reports={(r['type'],r['currency']):r for r in result['reports']}
    assert reports[('P','CNY')]['positiveRevenueTotal'] is None
    assert {r['name']:r['ratio'] for r in reports[('P','CNY')]['items']}=={'甲':None,'乙':None}
    assert reports[('D','CNY')]['positiveRevenueTotal'] is None
    assert reports[('P','USD')]['positiveRevenueTotal'] is None
    assert result['visibility']=='report-period-only' and result['historyComplete'] is False
    assert 'visibleAt' not in str(result)


def test_latest_flag_then_same_version_conflicts_and_duplicate_rows_are_not_summed():
    rows=[row(sales=100),row(sales=200,flag='1'),row(sales=200,flag='1')]
    result=project(rows)['reports'][0]
    assert result['items'][0]['revenue']==200 and result['positiveRevenueTotal'] is None
    conflicted=project([*rows,row(sales=300,flag='1')])['reports'][0]
    assert conflicted['items'][0]['revenue'] is None and conflicted['items'][0]['ratio'] is None
    assert '冲突' in conflicted['items'][0]['reason']
    assert project(rows)==project(list(reversed(rows)))


def test_missing_name_currency_or_sales_and_nonpositive_revenue_never_become_fake_slices():
    result=project([row(None,500),row('零',0),row('负',-100),row('缺',None),row('正常',100),row('币种缺',500,currency=None)])
    cny=next(r for r in result['reports'] if r['currency']=='CNY')
    assert cny['positiveRevenueTotal'] is None
    by_name={r['name']:r for r in cny['items']}
    assert by_name['零']['revenue']==0 and by_name['负']['revenue']==-100
    assert all(by_name[n]['ratio'] is None for n in (None,'零','负','缺'))
    assert cny['reasons']
    missing=next(r for r in result['reports'] if r['currency'] is None)
    assert missing['positiveRevenueTotal'] is None and missing['items'][0]['ratio'] is None


def test_different_labels_with_same_amount_are_not_deduplicated_as_overlap_is_unknown():
    report=project([row('其他产品',10),row('其他主营业务',10)])['reports'][0]
    assert len(report['items'])==2 and report['positiveRevenueTotal'] is None
    assert all(r['ratio'] is None for r in report['items'])
    assert report['groups'][0]['status'] == 'details'


def test_empty_input_is_missing_and_periods_are_kept_separate():
    assert project([])['reports']==[]
    result=project([row(period='20250630'),row(period='20251231')])
    assert [r['period'] for r in result['reports']]==['2025-06-30','2025-12-31']


def test_currency_codes_are_normalized_without_guessing_missing_currency():
    reports=project([row(currency=' cny '),row('乙',100,currency='CNY')])['reports']
    assert len(reports)==1 and reports[0]['currency']=='CNY'


def test_numeric_and_string_update_flags_are_same_version_not_arbitrary_winners():
    report=project([row(sales=100,flag=0),row(sales=200,flag='0')])['reports'][0]
    assert report['items'][0]['revenue'] is None


def test_accepted_integral_float_flags_are_same_version_as_integer_and_string():
    for flag in (0,1):
        report=project([row(sales=100,flag=float(flag)),row(sales=200,flag=str(flag))])['reports'][0]
        assert report['items'][0]['revenue'] is None


def test_reported_parent_and_direct_children_are_the_only_pie_inputs():
    items = [('地区', 40528509770.23), ('酒类产品', 37103992232.59),
             ('酒类产品：东部片区', 12831595450.04), ('酒类产品：北部片区', 6447448102.11),
             ('酒类产品：南部片区', 17824948680.44), ('非酒类产品', 3424517537.64)]
    rows = [row(name, amount, kind='D') for name, amount in items]
    result = project(rows)
    assert result['compositionVersion'] == 2
    root, region = result['reports'][0]['groups']
    assert root['status'] == 'details'
    assert region['status'] == 'verified' and region['dimension'] == 'region'
    assert region['denominatorRevenue'] == 37103992232.59
    assert len(region['members']) == 3
    assert round(sum(m['ratio'] for m in region['members']), 8) == 100
    without_parent = project([r for r in rows if r['bz_item'] != '酒类产品'] + [row('酒类产品', 37103992232.59)])
    assert all(g['status'] == 'details' for r in without_parent['reports'] for g in r['groups'])


def test_product_and_bank_examples():
    cases = [
        ([('产品', 40528509770.23), ('酒类产品', 37103992232.59), ('非酒类产品', 3424517537.64)], '酒类产品', 91.55),
        ([('产品', 172054171890.91), ('茅台酒', 146499906480.49), ('其他系列酒', 22274678707.16),
          ('其他业务', 63517327.14), ('合计特别调整', 3216069376.12)], '茅台酒', 85.15)]
    for items, name, expected in cases:
        group = project([row(n, v) for n, v in items])['reports'][0]['groups'][0]
        assert group['status'] == 'verified'
        assert round(next(m['ratio'] for m in group['members'] if m['name'] == name), 2) == expected
    items = [('产品', 100), ('利息净收入', 88021), ('公允价值变动损益', -87921),
             ('利息净收入：利息收入', 169863), ('利息净收入:利息支出', -81842)]
    report = project([row(n, v) for n, v in items])['reports'][0]
    assert all(g['status'] == 'details' for g in report['groups'])
    assert report['groups'][1]['denominatorRevenue'] == 88021


def test_overlapping_or_unidentified_regions_and_imbalances_remain_details():
    for names in [('中国', '华东'), ('东部片区', '直销'), ('未知甲', '未知乙')]:
        group = project([row(n, v, kind='D') for n, v in [('地区', 100), (names[0], 50), (names[1], 50)]])['reports'][0]['groups'][0]
        assert group['status'] == 'details'
    for total, expected in [(100, 'verified'), (101, 'details')]:
        report = project([row(n, v, kind='D') for n, v in [('地区', total), ('境内', 100), ('境外', 0)]])['reports'][0]
        assert report['groups'][0]['status'] == expected
def test_regional_revenue_mixed_with_product_revenue_remains_unverified():
    for geo in ('华东地区收入', '上海收入', '华东产品收入'):
        group = project([row('产品',100),row(geo,40),row('白酒产品',60)])['reports'][0]['groups'][0]
        assert group['status'] == 'details'
def test_explicit_root_prefix_uses_the_disclosed_root():
    for kind, root, names in [('D','地区',('境内','境外')), ('P','产品',('白酒产品','其他产品'))]:
        rows = [row(root,100,kind=kind)] + [row(root+'：'+n,50,kind=kind) for n in names]
        groups = project(rows)['reports'][0]['groups']
        assert len(groups) == 1 and groups[0]['status'] == 'verified'
        assert groups[0]['denominatorName'] == root
