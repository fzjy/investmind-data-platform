from importlib import import_module

import pytest


def module():
    return import_module('app.macro.official_macro_normalize')


REPORT = 'https://www.pbc.gov.cn/diaochatongjisi/116219/116225/2026072412345678901/index.html'


def report(amount='53.08', direction='下降', change='1.2', title='2026年二季度金融机构贷款投向统计报告'):
    return f'<h1>{title}</h1><p>二季度末，人民币房地产贷款余额{amount}万亿元，同比{direction}{change}%。个人住房贷款余额30万亿元，同比下降2%。房地产开发贷款余额10万亿元，同比增长5%。</p>'.encode()


def test_total_rmb_property_balance_and_yoy_not_developer_or_individual():
    result = module().parse_property_loan_report(report(), '2026-Q2')
    assert result['period'] == '2026-06-30' and result['role'] == 'property-report'
    points = {p['seriesId']: p for p in result['points']}
    assert points['cn.credit.property_loans_balance']['value'] == '530800'
    assert points['cn.credit.property_loans_balance']['unit'] == '亿元'
    assert points['cn.credit.property_loans_yoy']['value'] == '-1.2'
    assert points['cn.credit.property_loans_yoy']['unit'] == '%'
    assert all(p['frequency'] == 'quarterly' and p['period'] == '2026-06-30' for p in points.values())


@pytest.mark.parametrize('direction,change,expected', [('增长','2.5','2.5'), ('下降','0','0')])
def test_signed_yoy_and_zero(direction,change,expected):
    assert module().parse_property_loan_report(report(direction=direction,change=change), '2026-Q2')['points'][1]['value'] == expected


@pytest.mark.parametrize('body,quarter', [
    (report(), '2026-Q1'), (report(), '2026-Q5'), (report(), '2026-06'),
    (report(title='2025年二季度金融机构贷款投向统计报告'), '2026-Q2'),
    (report().replace('人民币房地产贷款余额'.encode(), '个人住房贷款余额'.encode()), '2026-Q2'),
    (report().replace('人民币房地产贷款余额'.encode(), '人民币房地产开发贷款余额'.encode()), '2026-Q2'),
    (report().replace('同比下降1.2%'.encode(), '比上季末下降1.2%'.encode()), '2026-Q2'),
    (report(amount='1<br>2'), '2026-Q2'), (report(amount='-1'), '2026-Q2'),
    (report(change='-1'), '2026-Q2'), (report(change='101'), '2026-Q2'),
    (report()+report(), '2026-Q2'),
])
def test_other_scope_period_non_yoy_bad_or_ambiguous_values_fail_closed(body,quarter):
    with pytest.raises(ValueError):
        module().parse_property_loan_report(body,quarter)


def directory(items):
    return ('<h1>调查统计司统计报告</h1><ul>'+''.join(f'<li><a href="{url}">{label}</a></li>' for url,label in items)+'</ul>').encode()


def test_directory_uses_only_actually_observed_original_report_and_latest_completed_quarter():
    body = directory([(REPORT,'2026年二季度金融机构贷款投向统计报告'),
                      ('/diaochatongjisi/116219/116225/2026102412345678901/index.html','2026年三季度金融机构贷款投向统计报告')])
    result = module().parse_property_directory(body, '2026-Q3', max_candidates=2, quarter_fallbacks=1)
    assert result['selected'] == {'url': 'https://www.pbc.gov.cn/diaochatongjisi/116219/116225/2026102412345678901/index.html', 'quarter': '2026-Q3'}
    assert result['candidateCount'] == 2 and len(result['rawSha256']) == 64


def test_directory_one_quarter_fallback_and_no_recursive_search():
    body=directory([(REPORT,'2026年二季度金融机构贷款投向统计报告'), ('https://example.org/','其它内容')])
    assert module().parse_property_directory(body, '2026-Q3', max_candidates=2, quarter_fallbacks=1)['selected']['quarter'] == '2026-Q2'
    assert module().parse_property_directory(body, '2026-Q3', max_candidates=2, quarter_fallbacks=0)['selected'] is None
    assert module().parse_property_directory(body, '2026-Q4', max_candidates=2, quarter_fallbacks=1)['selected'] is None


@pytest.mark.parametrize('url', [
    'http://www.pbc.gov.cn/diaochatongjisi/116219/116225/2026072412345678901/index.html',
    REPORT+'?redirect=x', REPORT+'#x', REPORT.replace('www.pbc.gov.cn','evil.example'),
    REPORT.replace('/116225/','/../116225/'), 'javascript:alert(1)',
])
def test_matching_report_link_must_be_exact_closed_origin_without_query_or_fragment(url):
    with pytest.raises(ValueError):
        module().parse_property_directory(directory([(url,'2026年二季度金融机构贷款投向统计报告')]), '2026-Q3', max_candidates=2, quarter_fallbacks=1)


def test_duplicate_quarter_distinct_urls_or_too_many_candidates_rejected():
    with pytest.raises(ValueError):
        module().parse_property_directory(directory([(REPORT,'2026年二季度金融机构贷款投向统计报告'),
            (REPORT.replace('241234','251234'),'2026年二季度金融机构贷款投向统计报告')]), '2026-Q3', max_candidates=2, quarter_fallbacks=1)
    with pytest.raises(ValueError):
        module().parse_property_directory(directory([(REPORT,'2026年二季度金融机构贷款投向统计报告'),
            (REPORT.replace('241234','251234'),'2026年三季度金融机构贷款投向统计报告')]), '2026-Q3', max_candidates=1, quarter_fallbacks=1)


def test_script_fake_links_ignored_and_exact_duplicate_anchor_deduplicated():
    one=directory([(REPORT,'2026年二季度金融机构贷款投向统计报告')])
    result=module().parse_property_directory(one+one+b'<script>'+one+b'</script>', '2026-Q3', max_candidates=2, quarter_fallbacks=1)
    assert result['candidateCount'] == 1
