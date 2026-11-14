from datetime import date
from decimal import Decimal
import json

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.connectors.macro import nbs_history as history
from app.connectors.macro.contracts import ProviderSchemaError
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog


OLD_URL = 'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1899420.html'
OLD_TITLE = '2017年1月份居民消费价格同比上涨2.5%'
OLD_BODY = f'''<meta name="ArticleTitle" content="{OLD_TITLE}">
<meta name="PubDate" content="2017/02/14 09:30">
<p>2017年1月份，全国居民消费价格总水平同比上涨2.5%。</p>
<table><tr><td></td><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>
<tr><td>其中：不包括食品和能源</td><td>0.6</td><td>2.2</td></tr></table>'''


def locator(**updates):
    return history.NbsReleaseLocator.model_validate({
        'seriesId': 'cn.cpi.core_yoy', 'url': OLD_URL, 'title': OLD_TITLE, **updates,
    })


def test_old_cpi_body_uses_core_yoy_not_headline_or_mom():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.cpi.core_yoy')
    row = history.parse_history_release(locator(), series.sources[0], OLD_BODY)
    assert row.observed_at == date(2017, 1, 1)
    assert row.value == Decimal('2.2')
    assert row.released_at.isoformat() == '2017-02-14T01:30:00+00:00'


def test_old_cpi_mismatched_body_period_is_rejected():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.cpi.core_yoy')
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(locator(), series.sources[0], OLD_BODY.replace('2017年1月份，全国', '2017年2月份，全国'))


@pytest.mark.parametrize('update', [
    {'url': 'https://evil.test/sj/zxfb/202302/t20230203_1899420.html'},
    {'url': OLD_URL + '?token=secret'}, {'url': OLD_URL + '#x'},
    {'url': OLD_URL.replace('www.stats.gov.cn', 'user@www.stats.gov.cn')},
    {'seriesId': 'cn.cpi.yoy'}, {'title': '2017年13月份居民消费价格同比上涨2.5%'},
    {'seriesId': 'cn.industry.value_added_yoy'}, {'extra': 'secret'},
])
def test_release_identity_rejects_untrusted_inputs(update):
    with pytest.raises(ValidationError):
        locator(**update)


def test_discovery_finds_both_series_and_fingerprint_ignores_layout():
    industry = 'https://www.stats.gov.cn/sj/zxfb/202609/t20260915_1965308.html'
    links = f'<a href="{OLD_URL}" title="{OLD_TITLE}">cpi</a><a href="{industry}" title="2026年8月份规模以上工业增加值增长5.2%">industry</a>'
    page = links + '<script>createPageHTML(67,0,"index","html");</script>'
    count, releases, fingerprint = history.discover_history_page(page, 0, date(2016, 9, 1), date(2026, 8, 31))
    assert count == 67
    assert [(r.series_id, r.observed_at) for r in releases] == [
        ('cn.cpi.core_yoy', date(2017, 1, 1)), ('cn.industry.value_added_yoy', date(2026, 8, 1)),
    ]
    assert history.discover_history_page(page.replace('</a>', '</a>\n '), 0, date(2016, 9, 1), date(2026, 8, 31))[2] == fingerprint


@pytest.mark.parametrize('body', ['', 'createPageHTML(0,0,"index","html")', 'createPageHTML(1001,0,"index","html")', 'createPageHTML(3,1,"index","html")'])
def test_invalid_directory_is_rejected(body):
    with pytest.raises(ProviderSchemaError):
        history.discover_history_page(body, 0, date(2016, 9, 1), date(2026, 8, 31))


def test_manifest_is_validated_before_use(tmp_path):
    path = tmp_path / 'releases.json'
    path.write_text(json.dumps({'schemaVersion': '1.0', 'releases': [locator().model_dump(by_alias=True)]}), encoding='utf-8')
    assert history.load_history_releases(path) == (locator(),)
    path.write_text(json.dumps({'schemaVersion': '1.0', 'releases': [{'seriesId': 'cn.cpi.core_yoy', 'url': 'https://evil.test', 'title': OLD_TITLE}]}), encoding='utf-8')
    with pytest.raises(ValueError):
        history.load_history_releases(path)


@pytest.mark.parametrize('limit', [0, 1, 101])
def test_settings_reject_invalid_history_budget(limit):
    with pytest.raises(ValidationError):
        Settings(macro_backfill_request_limit=limit)


def test_settings_history_defaults_and_end_configuration():
    settings = Settings(macro_backfill_end='2026-08-31')
    assert settings.macro_backfill_request_limit == 12
    assert settings.macro_backfill_end == date(2026, 8, 31)


def test_migrated_old_report_requires_title_heading_and_scoped_publication_date():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.cpi.core_yoy')
    legacy = f'<title>{OLD_TITLE} - 国家统计局</title><div class="detail-title"><h1>{OLD_TITLE}</h1><div class="detail-title-des"><h2><p>2017/02/14 09:30</p></h2></div></div>' + OLD_BODY[OLD_BODY.index('<p>'):]
    row = history.parse_history_release(locator(), series.sources[0], legacy)
    assert row.value == Decimal('2.2')
    assert row.released_at.isoformat() == '2017-02-14T01:30:00+00:00'


def test_legacy_publication_date_is_inside_h2_not_adjacent_font_or_share_controls():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.cpi.core_yoy')
    legacy = f'<title>{OLD_TITLE} - 国家统计局</title><div class="detail-title"><h1>{OLD_TITLE}</h1><div class="detail-title-des"><h2><p>2017/02/14 09:30</p></h2><h2><p class="ft">Aa</p><p class="share">分享到：</p></h2></div></div>' + OLD_BODY[OLD_BODY.index('<p>'):]
    assert history.parse_history_release(locator(), series.sources[0], legacy).value == Decimal('2.2')


@pytest.mark.parametrize('change', ['heading', 'date_scope', 'title', 'partial_meta'])
def test_legacy_identity_does_not_accept_ambiguous_page_metadata(change):
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.cpi.core_yoy')
    legacy = f'<title>{OLD_TITLE} - 国家统计局</title><div class="detail-title"><h1>{OLD_TITLE}</h1><div class="detail-title-des"><p>2017/02/14 09:30</p></div></div>' + OLD_BODY[OLD_BODY.index('<p>'):]
    if change == 'heading':
        legacy = legacy.replace(f'<h1>{OLD_TITLE}', '<h1>other')
    elif change == 'date_scope':
        legacy = legacy.replace('detail-title-des', 'unrelated')
    elif change == 'title':
        legacy = legacy.replace(f'<title>{OLD_TITLE}', '<title>other')
    else:
        legacy += '<meta name="ArticleTitle" content="wrong">'
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(locator(), series.sources[0], legacy)


def test_old_cpi_rowspan_header_does_not_shift_yoy_into_mom_column():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.cpi.core_yoy')
    body = OLD_BODY.replace('<tr><td></td><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>', '<tr><td colspan="3">2017年1月居民消费价格主要数据</td></tr><tr><td rowspan="2"></td><td colspan="2">1月</td></tr><tr><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>')
    assert history.parse_history_release(locator(), series.sources[0], body).value == Decimal('2.2')


def test_rowspan_header_with_wrong_month_is_rejected():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.cpi.core_yoy')
    body = OLD_BODY.replace('<tr><td></td><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>', '<tr><td rowspan="2"></td><td colspan="2">2月</td></tr><tr><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>')
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(locator(), series.sources[0], body)


def test_old_cpi_rowspan_with_cumulative_column_returns_monthly_yoy():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.cpi.core_yoy')
    release = locator(title='2016年9月份居民消费价格同比上涨1.9%', url='https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1899283.html')
    body = '''<meta name="ArticleTitle" content="2016年9月份居民消费价格同比上涨1.9%"><meta name="PubDate" content="2016/10/14 09:30"><p>2016年9月份，全国居民消费价格总水平同比上涨1.9%。</p>
    <table><tr><td colspan="4">2016年9月居民消费价格主要数据</td></tr><tr><td rowspan="2"></td><td colspan="2">9月</td><td>1-9月</td></tr><tr><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td><td>涨跌幅（%）</td></tr><tr><td>其中：不包括食品和能源</td><td>0.4</td><td>1.7</td><td>1.6</td></tr></table>'''
    assert history.parse_history_release(release, series.sources[0], body).value == Decimal('1.7')


def test_rowspan_caption_with_wrong_year_is_rejected():
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require('cn.cpi.core_yoy')
    body = OLD_BODY.replace('<tr><td></td><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>', '<tr><td colspan="3">2016年1月居民消费价格主要数据</td></tr><tr><td rowspan="2"></td><td colspan="2">1月</td></tr><tr><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>')
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(locator(), series.sources[0], body)


def _external_caption_cpi_body(month=12, annual_label='2016年', caption='2016年12月'):
    return f'''<meta name="ArticleTitle" content="2016年{month}月份居民消费价格同比上涨2.1%">
    <meta name="PubDate" content="2017/01/10 09:30">
    <p>2016年{month}月份，全国居民消费价格总水平同比上涨2.1%。</p>
    <p>{caption}居民消费价格主要数据</p>
    <table><tr><td rowspan="2"></td><td colspan="2">{month}月</td><td>{annual_label}</td></tr>
    <tr><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td><td>涨跌幅（%）</td></tr>
    <tr><td>其中：不包括食品和能源</td><td>0.1</td><td>1.9</td><td>1.6</td></tr></table>'''


def test_december_external_caption_and_annual_column_uses_monthly_core_yoy():
    release = locator(title='2016年12月份居民消费价格同比上涨2.1%',
                      url='https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1899387.html')
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id)
    row = history.parse_history_release(release, series.sources[0], _external_caption_cpi_body())
    assert row.value == Decimal('1.9')
    assert row.observed_at == date(2016, 12, 1)
    assert row.released_at.isoformat() == '2017-01-10T01:30:00+00:00'


def test_cumulative_column_with_external_caption_uses_monthly_core_yoy():
    release = locator(title='2016年9月份居民消费价格同比上涨2.1%')
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id)
    body = _external_caption_cpi_body(9, '1-9月', '2016年9月').replace('<td>1.9</td>', '<td>1.7</td>')
    assert history.parse_history_release(release, series.sources[0], body).value == Decimal('1.7')


@pytest.mark.parametrize('mutation', ['wrong_year', 'wrong_month', 'annual_year', 'annual_non_december',
                                      'missing_caption', 'wrong_internal_caption', 'wrong_mobile_caption',
                                      'native_caption', 'late_caption_row', 'native_mobile_caption'])
def test_external_caption_cpi_rejects_wrong_period_or_annual_layout(mutation):
    month = 11 if mutation == 'annual_non_december' else 12
    release = locator(title=f'2016年{month}月份居民消费价格同比上涨2.1%')
    series = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id)
    body = _external_caption_cpi_body(month, caption=f'2016年{month}月')
    if mutation in ('wrong_year', 'wrong_month', 'missing_caption'):
        replacement = {'wrong_year': '2015年12月居民消费价格主要数据',
                       'wrong_month': '2016年11月居民消费价格主要数据', 'missing_caption': ''}[mutation]
        body = body.replace(f'2016年{month}月居民消费价格主要数据', replacement)
    elif mutation == 'annual_year':
        body = body.replace('<td>2016年</td>', '<td>2015年</td>')
    elif mutation == 'wrong_internal_caption':
        body = body.replace('<table>', '<table><tr><td colspan="4">2015年12月居民消费价格主要数据</td></tr>')
    elif mutation == 'native_caption':
        body = body.replace('<table>', '<table><caption>2015年12月居民消费价格主要数据</caption>')
    elif mutation == 'late_caption_row':
        body = body.replace('</table>', '<tr><td colspan="4">2015年12月居民消费价格主要数据</td></tr></table>')
    elif mutation == 'native_mobile_caption':
        table = body[body.index('<table>'):].replace('<table>', '<table><caption>2015年12月居民消费价格主要数据</caption>')
        body += '<p>2016年12月居民消费价格主要数据</p>' + table
    elif mutation == 'wrong_mobile_caption':
        table = body[body.index('<table>'):]
        body += '<p>2015年12月居民消费价格主要数据</p>' + table
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, series.sources[0], body)


def _historical_2024_report(series_id):
    if series_id == 'cn.cpi.core_yoy':
        title = '2024年5月份居民消费价格同比上涨0.3%'
        url = 'https://www.stats.gov.cn/sj/zxfb/202406/t20240612_1954519.html'
        published = '2024/06/12 09:30'
        data = '''<p>2024年5月份，全国居民消费价格同比上涨0.3%。</p>
        <table><tr><td></td><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>
        <tr><td>其中：不包括食品和能源</td><td>-0.2</td><td>0.6</td></tr></table>'''
    else:
        title = '2024年5月份规模以上工业增加值增长5.6%'
        url = 'https://www.stats.gov.cn/sj/zxfb/202406/t20240617_1954712.html'
        published = '2024/06/17 10:00'
        data = '<p>5月份，规模以上工业增加值同比实际增长5.6%（下同）。</p>'
    release = history.NbsReleaseLocator(series_id=series_id, url=url, title=title)
    body = f'''<title>{title} - 国家统计局</title><meta name="viewport" content="width=device-width">
    <div class="detail-title"><h1>{title}</h1><div class="detail-title-des">
    <h2><p>{published}</p></h2><h2><p class="ft">Aa</p></h2></div></div>{data}'''
    return release, body


@pytest.mark.parametrize(('series_id', 'value', 'published'), [
    ('cn.cpi.core_yoy', '0.6', '2024-06-12T01:30:00+00:00'),
    ('cn.industry.value_added_yoy', '5.6', '2024-06-17T02:00:00+00:00'),
])
def test_2024_history_uses_verified_dom_identity_when_both_metadata_absent(series_id, value, published):
    release, body = _historical_2024_report(series_id)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(series_id).sources[0]
    row = history.parse_history_release(release, source, body)
    assert row.observed_at == date(2024, 5, 1)
    assert row.value == Decimal(value)
    assert row.released_at.isoformat() == published


@pytest.mark.parametrize('mutation', ['partial_meta', 'empty_meta', 'wrong_meta', 'wrong_heading',
                                      'wrong_date_scope', 'latest_directory', 'invalid_month_directory',
                                      'title_no_content', 'date_no_content', 'bare_content'])
def test_2024_history_does_not_override_partial_metadata_or_unverified_identity(mutation):
    release, body = _historical_2024_report('cn.cpi.core_yoy')
    if mutation == 'partial_meta':
        body += f'<meta name="ArticleTitle" content="{release.title}">'
    elif mutation == 'empty_meta':
        body += '<meta name="ArticleTitle" content=""><meta name="PubDate" content="">'
    elif mutation == 'wrong_meta':
        body += '<meta name="ArticleTitle" content="wrong"><meta name="PubDate" content="2024/06/12 09:30">'
    elif mutation == 'title_no_content':
        body += '<meta name="ArticleTitle">'
    elif mutation == 'date_no_content':
        body += '<meta name="PubDate">'
    elif mutation == 'bare_content':
        body += '<meta name="ArticleTitle" content><meta name="PubDate" content>'
    elif mutation == 'wrong_heading':
        body = body.replace(f'<h1>{release.title}', '<h1>2024年4月份居民消费价格同比上涨0.3%')
    elif mutation == 'wrong_date_scope':
        body = body.replace('detail-title-des', 'unrelated')
    else:
        path = '202501' if mutation == 'latest_directory' else '202413'
        release = locator(title=release.title, url=release.url.replace('/202406/', f'/{path}/'))
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('kind', ['sep', 'half'])
@pytest.mark.parametrize('location', ['inside', 'before'])
@pytest.mark.parametrize('period', ['下半年', '第四季度', '度'])
def test_legacy_cpi_review_rejects_unknown_caption_period(kind, location, period):
    # 好月题不能掩盖同副本额外出现的半年/季度/年度题。
    identity, body, table = _legacy_sep_or_half_cpi_report(kind)
    year = 2017 if kind == 'sep' else 2018
    caption = f'{year}年{period}居民消费价格主要数据'
    mobile = (table.replace('<table>', f'<table><caption>{caption}</caption>')
              if location == 'inside' else f'<p>{caption}</p>' + table)
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body + mobile)


@pytest.mark.parametrize('kind', ['sep', 'half'])
@pytest.mark.parametrize('header', ['basis_units', 'annual'])
def test_legacy_cpi_review_rejects_extra_basis_or_annual_headers(kind, header):
    identity, body, table = _legacy_sep_or_half_cpi_report(kind)
    year = 2017 if kind == 'sep' else 2018
    cells = ('<td>环比（%）</td><td>同比（%）</td><td>累计同比（%）</td>'
             if header == 'basis_units' else f'<td>{year}年度</td>' * 3)
    mobile = table.replace('<tr><td>居民消费价格',
        '<tr><td></td>' + cells + '</tr><tr><td>居民消费价格')
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body + mobile)


@pytest.mark.parametrize('kind', ['sep', 'half'])
def test_legacy_cpi_review_rejects_zero_colspan_core_copy(kind):
    # 原始核心标签仍存在，不能因colspan=0归一化消失而跳过冲突副本。
    identity, body, table = _legacy_sep_or_half_cpi_report(kind)
    core = ('<td>0.5</td><td>2.3</td><td>2.1</td>' if kind == 'sep'
            else '<td>0.1</td><td>1.9</td><td>2.0</td>')
    mobile = table.replace('<td>其中：不包括食品和能源</td>',
        '<td colspan="0">其中：不包括食品和能源</td>')
    mobile = mobile.replace(core, '<td>99.7</td><td>99.9</td><td>99.8</td>')
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body + mobile)


@pytest.mark.parametrize('kind', ['sep', 'half'])
def test_legacy_cpi_review_rejects_upstream_rowspan_into_core(kind):
    # 其他行占据下一行的列时，核心行原始数组下标不再证明列位置。
    identity, body, table = _legacy_sep_or_half_cpi_report(kind)
    mobile = table.replace('<tr><td>其中：不包括食品和能源',
        '<tr><td>食品</td><td rowspan="2">99.7</td><td>99.9</td><td>99.8</td></tr>'
        '<tr><td>其中：不包括食品和能源')
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body + mobile)


def _six_column_industry_report():
    release = history.NbsReleaseLocator(series_id='cn.industry.value_added_yoy',
        url='https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1901461.html',
        title='2022年1—4月份规模以上工业增加值增长4.0%')
    table = '''<p>2022年4月份规模以上工业生产主要数据</p>
    <table><tr><td rowspan="2"></td><td colspan="2">4月</td><td colspan="2">1—4月</td><td></td></tr>
    <tr><td>绝对量</td><td>同比增长（%）</td><td>绝对量</td><td>同比增长（%）</td><td>同比增长（%）</td></tr>
    <tr><td>规模以上工业增加值</td><td>…</td><td>-2.9</td><td>…</td><td>4.0</td><td></td></tr></table>'''
    body = f'''<meta name="ArticleTitle" content="{release.title}"><meta name="PubDate" content="2022/05/16 10:00">
    <p>1—4月份，规模以上工业增加值同比实际增长4.0%（以下增加值增速均为扣除价格因素的实际增长率）。
    4月份，规模以上工业增加值同比下降2.9%。从环比看，4月份，规模以上工业增加值比上月下降7.08%。</p>{table}'''
    return release, body, table


@pytest.mark.parametrize('mobile_copy', [False, True])
def test_history_industry_six_column_blank_tail_returns_monthly_decline(mobile_copy):
    release, body, table = _six_column_industry_report()
    if mobile_copy:
        body += table
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    row = history.parse_history_release(release, source, body)
    assert row.value == Decimal('-2.9')
    assert row.observed_at == date(2022, 4, 1)
    assert row.released_at.isoformat() == '2022-05-16T02:00:00+00:00'


@pytest.mark.parametrize('mutation', ['nonempty_tail', 'unknown_header', 'wrong_month', 'wrong_year',
                                      'mobile_conflict', 'missing_tail', 'extra_column'])
def test_history_industry_six_column_rejects_unknown_or_conflicting_evidence(mutation):
    release, body, table = _six_column_industry_report()
    if mutation == 'nonempty_tail':
        body = body.replace('<td>4.0</td><td></td>', '<td>4.0</td><td>99</td>')
    elif mutation == 'unknown_header':
        body = body.replace('<td>同比增长（%）</td></tr>', '<td>环比增长（%）</td></tr>')
    elif mutation == 'wrong_month':
        body = body.replace('colspan="2">4月', 'colspan="2">3月')
    elif mutation == 'wrong_year':
        body = body.replace('2022年4月份规模以上工业生产主要数据', '2021年4月份规模以上工业生产主要数据')
    elif mutation == 'mobile_conflict':
        body += table.replace('<td>-2.9</td>', '<td>4.0</td>')
    elif mutation == 'missing_tail':
        body = body.replace('<td>4.0</td><td></td>', '<td>4.0</td>')
    else:
        body = body.replace('<td>4.0</td><td></td>', '<td>4.0</td><td></td><td></td>')
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


def _alternate_cpi_report(month):
    # 手核对官方主表：核心同比均0.4，总同比/环比及累计是不同指标。
    if month == 1:
        url = 'https://www.stats.gov.cn/sj/zxfb/202402/t20240208_1947623.html'
        title = '2024年1月份居民消费价格环比上涨0.3%'
        published = '2024/02/08 09:30'
        metadata = ''
        text = ('2024年1月份，受节日效应影响居民消费需求持续增加，全国居民消费价格环比上涨0.3%，已连续两个月上涨。'
                '1月份，受上年同期春节错月高基数影响，全国居民消费价格同比下降0.8%。')
        total, cumulative = '-0.8', ''
    else:
        url = 'https://www.stats.gov.cn/sj/zxfb/202408/t20240809_1955948.html'
        title = '2024年7月份居民消费价格上涨0.5%'
        published = '2024/08/09 09:30'
        metadata = f'<meta name="ArticleTitle" content="{title}"><meta name="PubDate" content="{published}">'
        text = '2024年7月份，全国居民消费价格同比上涨0.5%。7月份，全国居民消费价格环比上涨0.5%。'
        total, cumulative = '0.5', '<td>1—7月同比涨跌幅（%）</td>'
    table = f'''<p>2024年{month}月份居民消费价格主要数据</p><table>
    <tr><td></td><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td>{cumulative}</tr>
    <tr><td>居民消费价格</td><td>{'0.3' if month == 1 else '0.5'}</td><td>{total}</td>{'<td>0.2</td>' if month == 7 else ''}</tr>
    <tr><td>其中：不包括食品和能源</td><td>0.3</td><td>0.4</td>{'<td>0.6</td>' if month == 7 else ''}</tr></table>'''
    body = (f'<title>{title} - 国家统计局</title>{metadata}<div class="detail-title"><h1>{title}</h1>'
            f'<div class="detail-title-des"><h2><p>{published}</p></h2></div></div><p>{text}</p>{table}{table}')
    return {'seriesId': 'cn.cpi.core_yoy', 'url': url, 'title': title}, body, table


@pytest.mark.parametrize('month', [1, 7])
def test_alternate_cpi_title_discovery_includes_monthly_core_report(month):
    identity, _, _ = _alternate_cpi_report(month)
    page = (f'<a href="{identity["url"]}" title="{identity["title"]}">CPI</a>'
            '<script>createPageHTML(67,0,"index","html");</script>')
    _, releases, _ = history.discover_history_page(page, 0, date(2024, 1, 1), date(2024, 12, 31))
    assert len(releases) == 1
    assert releases[0].observed_at == date(2024, month, 1)


@pytest.mark.parametrize(('month', 'published'), [(1, '2024-02-08T01:30:00+00:00'), (7, '2024-08-09T01:30:00+00:00')])
def test_alternate_cpi_report_returns_core_yoy_after_body_and_total_table_checks(month, published):
    identity, body, _ = _alternate_cpi_report(month)
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    row = history.parse_history_release(release, source, body)
    assert row.value == Decimal('0.4')
    assert row.observed_at == date(2024, month, 1)
    assert row.released_at.isoformat() == published


@pytest.mark.parametrize('month', [1, 7])
@pytest.mark.parametrize('mutation', ['wrong_year', 'wrong_month', 'no_year', 'no_yoy', 'no_mom',
    'body_conflict', 'wrong_total', 'missing_total', 'wrong_caption', 'mobile_conflict',
    'mobile_wrong_caption', 'wrong_header', 'nonfinite', 'row_width', 'cross_sentence'])
def test_alternate_cpi_report_rejects_unproven_or_conflicting_body_and_tables(month, mutation):
    identity, body, table = _alternate_cpi_report(month)
    if mutation == 'wrong_year':
        body = body.replace(f'<p>2024年{month}月份，', f'<p>2023年{month}月份，')
    elif mutation == 'wrong_month':
        body = body.replace(f'<p>2024年{month}月份，', '<p>2024年2月份，')
    elif mutation == 'no_year':
        body = body.replace(f'<p>2024年{month}月份，', f'<p>{month}月份，')
    elif mutation in {'no_yoy', 'no_mom'}:
        body = body.replace('全国居民消费价格' + ('同比' if mutation == 'no_yoy' else '环比'), '其他价格')
    elif mutation == 'body_conflict':
        body += f'<p>2024年{month}月份，全国居民消费价格同比上涨9.9%。</p>'
    elif mutation == 'wrong_total':
        body = body.replace('<td>居民消费价格</td><td>', '<td>居民消费价格</td><td>9')
    elif mutation == 'missing_total':
        body = body.replace('<td>居民消费价格</td>', '<td>城市</td>')
    elif mutation == 'wrong_caption':
        body = body.replace(f'2024年{month}月份居民消费价格主要数据', f'2023年{month}月份居民消费价格主要数据')
    elif mutation == 'mobile_conflict':
        body = body[:body.rfind(table)] + table.replace('<td>0.4</td>', '<td>0.6</td>')
    elif mutation == 'mobile_wrong_caption':
        body = body[:body.rfind(table)] + table.replace('2024年', '2023年')
    elif mutation == 'wrong_header':
        body = body.replace('<td>同比涨跌幅（%）</td>', '<td>环比涨跌幅（%）</td>')
    elif mutation == 'nonfinite':
        body = body.replace('<td>0.4</td>', '<td>NaN</td>')
    elif mutation == 'row_width':
        body = body.replace('<td>0.4</td>', '<td>0.4</td><td></td>')
    else:
        body = body.replace('，全国居民消费价格', '。全国居民消费价格')
    with pytest.raises((ValidationError, ProviderSchemaError)):
        release = history.NbsReleaseLocator.model_validate(identity)
        source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('title', ['2024年7月份居民消费价格上涨0.5%其他解释',
    '2024年13月份居民消费价格环比上涨0.3%', '2024年7月份居民消费价格上涨NaN%',
    '2024年7月份居民消费价格环比持平0.3%', '2024年7月份居民消费价格持平0.3%',
    '2024年7月份居民消费价格环比上涨'])
def test_alternate_cpi_identity_rejects_unknown_title_forms(title):
    identity, _, _ = _alternate_cpi_report(7)
    with pytest.raises(ValidationError):
        history.NbsReleaseLocator.model_validate({**identity, 'title': title})


@pytest.mark.parametrize('mutation', ['cumulative_body', 'cross_exclamation', 'cross_question',
    'invalid_core_cumulative', 'invalid_total_cumulative', 'cumulative_conflict',
    'core_mom_conflict', 'illegal_caption', 'extra_header'])
def test_alternate_cpi_review_rejects_disguised_period_and_mobile_evidence_conflicts(mutation):
    month = 1 if mutation.startswith('cross_') else 7
    identity, body, table = _alternate_cpi_report(month)
    if mutation == 'cumulative_body':
        body = body.replace('2024年7月份，全国居民消费价格同比', '2023年1—7月份，全国居民消费价格同比')
        body = body.replace('7月份，全国居民消费价格环比', '2024年7月份，全国居民消费价格环比')
    elif mutation.startswith('cross_'):
        boundary = '！' if mutation == 'cross_exclamation' else '？'
        body = body.replace('受节日效应影响居民消费需求持续增加，', f'受节日效应影响{boundary}另据去年同期报告，')
    else:
        mobile = table
        if mutation == 'invalid_core_cumulative':
            mobile = mobile.replace('<td>0.6</td>', '<td>NaN</td>')
        elif mutation == 'invalid_total_cumulative':
            mobile = mobile.replace('<td>0.2</td>', '<td>unknown</td>')
        elif mutation == 'cumulative_conflict':
            mobile = mobile.replace('<td>0.6</td>', '<td>9.9</td>')
        elif mutation == 'core_mom_conflict':
            mobile = mobile.replace('<td>其中：不包括食品和能源</td><td>0.3</td>', '<td>其中：不包括食品和能源</td><td>9.9</td>')
        elif mutation == 'illegal_caption':
            mobile = mobile.replace('<table>', '<table><caption>2024年13月份居民消费价格主要数据</caption>')
        else:
            mobile = mobile.replace('<tr><td>其中：不包括食品和能源',
                '<tr><td></td><td>同比涨跌幅（%）</td><td>环比涨跌幅（%）</td><td>1—7月同比涨跌幅（%）</td></tr><tr><td>其中：不包括食品和能源')
        body = body[:body.rfind(table)] + mobile
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


def _half_year_industry_report():
    identity = {'seriesId': 'cn.industry.value_added_yoy',
        'url': 'https://www.stats.gov.cn/sj/zxfb/202307/t20230715_1941268.html',
        'title': '2023年上半年规模以上工业增加值增长3.8%'}
    table = '''<p>2023年6月份及上半年规模以上工业生产主要数据</p><table>
    <tr><td rowspan="2">指标</td><td colspan="2">6月</td><td colspan="2">1—6月</td></tr>
    <tr><td>绝对量</td><td>同比增长（%）</td><td>绝对量</td><td>同比增长（%）</td></tr>
    <tr><td>规模以上工业增加值</td><td>…</td><td>4.4</td><td>…</td><td>3.8</td></tr></table>'''
    body = (f'<title>{identity["title"]} - 国家统计局</title><div class="detail-title"><h1>{identity["title"]}</h1>'
        '<div class="detail-title-des"><h2><p>2023/07/17 10:00</p></h2></div></div>'
        '<p>上半年，规模以上工业增加值同比增长3.8%（增加值增速均为扣除价格因素的实际增长率）。'
        '6月份，规模以上工业增加值同比实际增长4.4%。</p>' + table + table)
    return identity, body, table


def test_half_year_industry_discovery_identifies_june_not_cumulative_period():
    identity, _, _ = _half_year_industry_report()
    page = (f'<a href="{identity["url"]}" title="{identity["title"]}">工业</a>'
            '<script>createPageHTML(67,43,"index","html");</script>')
    _, releases, _ = history.discover_history_page(page, 43, date(2023, 1, 1), date(2023, 12, 31))
    assert len(releases) == 1
    assert releases[0].observed_at == date(2023, 6, 1)


def test_half_year_industry_report_reads_actual_june_value_not_half_year_value():
    identity, body, _ = _half_year_industry_report()
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    row = history.parse_history_release(release, source, body)
    assert row.observed_at == date(2023, 6, 1)
    assert row.value == Decimal('4.4')
    assert row.released_at.isoformat() == '2023-07-17T02:00:00+00:00'


@pytest.mark.parametrize('mutation', ['wrong_month', 'wrong_caption_year', 'missing_deflator',
    'no_actual_monthly', 'wrong_cumulative', 'mobile_conflict', 'wrong_header', 'nonfinite', 'missing_caption',
    'wrong_caption_template', 'illegal_inside_caption'])
def test_half_year_industry_rejects_unproven_or_conflicting_period_and_values(mutation):
    identity, body, table = _half_year_industry_report()
    if mutation == 'wrong_month':
        body = body.replace('6月份，', '5月份，')
    elif mutation == 'wrong_caption_year':
        body = body.replace('2023年6月份及上半年', '2022年6月份及上半年')
    elif mutation == 'missing_deflator':
        body = body.replace('（增加值增速均为扣除价格因素的实际增长率）', '')
    elif mutation == 'no_actual_monthly':
        body = body.replace('同比实际增长4.4%', '同比增长4.4%')
    elif mutation == 'wrong_cumulative':
        body = body.replace('同比增长3.8%', '同比增长4.4%')
    elif mutation == 'mobile_conflict':
        body = body[:body.rfind(table)] + table.replace('<td>4.4</td>', '<td>3.8</td>')
    elif mutation == 'wrong_header':
        body = body.replace('1—6月', '1—5月')
    elif mutation == 'nonfinite':
        body = body.replace('<td>4.4</td>', '<td>Infinity</td>')
    elif mutation == 'wrong_caption_template':
        body = body.replace('6月份及上半年规模以上工业生产主要数据', '6月份规模以上工业生产主要数据')
    elif mutation == 'illegal_inside_caption':
        body = body[:body.rfind(table)] + table.replace('<table>',
            '<table><caption>2023年13月份及上半年规模以上工业生产主要数据</caption>')
    else:
        body = body.replace('<p>2023年6月份及上半年规模以上工业生产主要数据</p>', '')
    with pytest.raises((ValidationError, ProviderSchemaError)):
        release = history.NbsReleaseLocator.model_validate(identity)
        source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('title', ['2023年下半年规模以上工业增加值增长3.8%',
    '2023年全年规模以上工业增加值增长3.8%', '2023年上半年规模以上工业增加值增长3.8%其他解释'])
def test_half_year_industry_does_not_guess_other_periods(title):
    identity, _, _ = _half_year_industry_report()
    with pytest.raises(ValidationError):
        history.NbsReleaseLocator.model_validate({**identity, 'title': title})


@pytest.mark.parametrize('mutation', ['wrong_standard_caption', 'annual_caption', 'conflicting_range_body',
    'invalid_range_body', 'wrong_body_year', 'wrong_monthly_body_year', 'wrong_range_body_year'])
def test_half_year_industry_review_rejects_hidden_caption_and_body_conflicts(mutation):
    identity, body, table = _half_year_industry_report()
    if mutation in {'wrong_standard_caption', 'annual_caption'}:
        caption = ('2022年6月份规模以上工业生产主要数据' if mutation == 'wrong_standard_caption'
                   else '2023年全年规模以上工业生产主要数据')
        body = body[:body.rfind(table)] + table.replace('<table>', f'<table><caption>{caption}</caption>')
    elif mutation in {'conflicting_range_body', 'invalid_range_body'}:
        value = '9.9' if mutation == 'conflicting_range_body' else 'NaN'
        body += f'<p>1—6月份，规模以上工业增加值同比实际增长{value}%。</p>'
    elif mutation == 'wrong_monthly_body_year':
        body = body.replace('6月份，规模以上工业增加值同比实际增长', '2022年6月份，规模以上工业增加值同比实际增长')
    elif mutation == 'wrong_range_body_year':
        body += '<p>2022年1—6月份，规模以上工业增加值同比实际增长3.8%。</p>'
    else:
        body = body.replace('<p>上半年，', '<p>2022年上半年，')
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


def _legacy_2018_cpi_report(month):
    # 官方列顺序手核对：月同比1.8/2.0，不是全年1.9/一季度2.1。
    if month == 12:
        identity = {'seriesId': 'cn.cpi.core_yoy',
            'url': 'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1900194.html',
            'title': '2018年12月份居民消费价格同比上涨1.9%'}
        published, headline = '2019/01/10 09:30', '1.9'
        table = '''<p>2018年12月份居民消费价格主要数据</p><table>
        <tr><td></td><td>环比涨跌（%）</td><td>同比涨跌（%）</td><td>2018年涨跌（%）</td></tr>
        <tr><td>居民消费价格</td><td>0.0</td><td>1.9</td><td>2.1</td></tr>
        <tr><td>其中：不包括食品和能源</td><td>0.1</td><td>1.8</td><td>1.9</td></tr></table>'''
    else:
        identity = {'seriesId': 'cn.cpi.core_yoy',
            'url': 'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1899887.html',
            'title': '2018年3月份居民消费价格同比上涨2.1%'}
        published, headline = '2018/04/11 09:30', '2.1'
        table = '''<table><tr><td colspan="4">2018年3月居民消费价格主要数据</td></tr>
        <tr><td rowspan="2"></td><td colspan="2">3月</td><td>一季度</td></tr>
        <tr><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>
        <tr><td>居民消费价格</td><td>-1.1</td><td>2.1</td><td>2.1</td></tr>
        <tr><td>其中：不包括食品和能源</td><td>-0.3</td><td>2.0</td><td>2.1</td></tr></table>'''
    body = (f'<meta name="ArticleTitle" content="{identity["title"]}"><meta name="PubDate" content="{published}">'
            f'<p>2018年{month}月份，全国居民消费价格同比上涨{headline}%。</p>' + table)
    return identity, body, table


@pytest.mark.parametrize(('month', 'value', 'published'), [
    (12, '1.8', '2019-01-10T01:30:00+00:00'), (3, '2.0', '2018-04-11T01:30:00+00:00')])
@pytest.mark.parametrize('mobile_copy', [False, True])
def test_legacy_2018_cpi_short_annual_and_quarter_headers_return_monthly_core(month, value, published, mobile_copy):
    identity, body, table = _legacy_2018_cpi_report(month)
    if mobile_copy:
        body += table
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    row = history.parse_history_release(release, source, body)
    assert row.value == Decimal(value)
    assert row.observed_at == date(2018, month, 1)
    assert row.released_at.isoformat() == published


@pytest.mark.parametrize('month', [12, 3])
@pytest.mark.parametrize('mutation', ['caption_year', 'caption_month', 'missing_caption', 'inside_caption',
    'wrong_period_header', 'unknown_header', 'extra_header', 'missing_total', 'wrong_total',
    'core_nonfinite', 'total_nonfinite', 'row_width', 'copy_core_mom', 'copy_core_yoy',
    'copy_core_cumulative', 'copy_total_cumulative', 'copy_bad_header'])
def test_legacy_2018_cpi_rejects_unproven_or_conflicting_layout_and_complete_rows(month, mutation):
    identity, body, table = _legacy_2018_cpi_report(month)
    mobile = table
    if mutation == 'caption_year':
        mobile = mobile.replace(f'2018年{month}月', f'2017年{month}月')
    elif mutation == 'caption_month':
        mobile = mobile.replace(f'2018年{month}月', '2018年13月')
    elif mutation == 'missing_caption':
        if month == 12:
            mobile = mobile[mobile.index('<table>'):]
        else:
            mobile = mobile.replace('<tr><td colspan="4">2018年3月居民消费价格主要数据</td></tr>', '')
    elif mutation == 'inside_caption':
        mobile = mobile.replace('<table>', f'<table><caption>2017年{month}月居民消费价格主要数据</caption>')
    elif mutation == 'wrong_period_header':
        mobile = mobile.replace('2018年涨跌', '2017年涨跌') if month == 12 else mobile.replace('一季度', '二季度')
    elif mutation in {'unknown_header', 'copy_bad_header'}:
        mobile = mobile.replace('环比涨跌', '未知涨跌')
    elif mutation == 'extra_header':
        mobile = mobile.replace('<tr><td>居民消费价格', '<tr><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr><tr><td>居民消费价格')
    elif mutation == 'missing_total':
        mobile = mobile.replace('<td>居民消费价格</td>', '<td>城市</td>')
    elif mutation == 'wrong_total':
        mobile = mobile.replace('<td>居民消费价格</td><td>', '<td>居民消费价格</td><td>0</td><td>9.9</td><td>0</td></tr><tr><td>城市</td><td>')
    elif mutation in {'core_nonfinite', 'total_nonfinite'}:
        value = ('1.8' if month == 12 else '2.0') if mutation == 'core_nonfinite' else '2.1'
        mobile = mobile.replace(f'<td>{value}</td>', '<td>NaN</td>')
    elif mutation == 'row_width':
        mobile = mobile.replace('</tr></table>', '<td></td></tr></table>')
    elif mutation.startswith('copy_core'):
        row = ('<td>0.1</td><td>1.8</td><td>1.9</td>' if month == 12
               else '<td>-0.3</td><td>2.0</td><td>2.1</td>')
        cells = row.split('</td>')[:-1]
        column = {'copy_core_mom': 0, 'copy_core_yoy': 1, 'copy_core_cumulative': 2}[mutation]
        cells[column] = '<td>9.9'
        mobile = mobile.replace(row, '</td>'.join(cells) + '</td>')
    else:
        row = ('<td>0.0</td><td>1.9</td><td>2.1</td>' if month == 12
               else '<td>-1.1</td><td>2.1</td><td>2.1</td>')
        mobile = mobile.replace(row, row[:row.rfind('<td>')] + '<td>9.9</td>')
    body += mobile
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('month', [12, 3])
@pytest.mark.parametrize('mutation', ['extra_period_header', 'shifted_mobile_value', 'shifted_mobile_year', 'shifted_same_table'])
def test_legacy_2018_cpi_review_rejects_hidden_period_headers_and_shifted_core_rows(month, mutation):
    identity, body, table = _legacy_2018_cpi_report(month)
    if mutation == 'extra_period_header':
        header = ('<tr><td></td><td>环比</td><td>同比</td><td>2017年</td></tr>' if month == 12
                  else '<tr><td></td><td colspan="2">4月</td><td>二季度</td></tr>')
        body = body.replace('<tr><td>居民消费价格', header + '<tr><td>居民消费价格')
        body = body.replace('<td>1.8</td>' if month == 12 else '<td>2.0</td>', '<td>9.9</td>')
    else:
        mobile = table.replace('<td>其中：不包括食品和能源</td>', '<td></td><td>其中：不包括食品和能源</td>')
        if mutation == 'shifted_mobile_year':
            mobile = mobile.replace(f'2018年{month}月', f'2017年{month}月')
        else:
            mobile = mobile.replace('<td>1.8</td>' if month == 12 else '<td>2.0</td>', '<td>9.9</td>')
        if mutation == 'shifted_same_table':
            core = mobile[mobile.index('<tr><td></td><td>其中：'):mobile.index('</table>')]
            body = body.replace('</table>', core + '</table>')
        else:
            body += mobile
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('mobile_copy', [False, True])
@pytest.mark.parametrize('span', ['', 'rowspan="3"', 'rowspan="1"'])
def test_legacy_2018_quarter_review_requires_verified_two_row_indicator_span(mobile_copy, span):
    identity, body, table = _legacy_2018_cpi_report(3)
    broken = table.replace('rowspan="2"', span)
    body = body + broken if mobile_copy else body.replace(table, broken)
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('month', [12, 3])
@pytest.mark.parametrize('period', ['13月', '第五季度', '10000年', '上半年'])
def test_legacy_2018_cpi_rejects_extra_unknown_or_illegal_period_header(month, period):
    identity, body, _ = _legacy_2018_cpi_report(month)
    body = body.replace('<tr><td>居民消费价格',
        f'<tr><td></td><td>{period}</td><td>{period}</td><td>{period}</td></tr><tr><td>居民消费价格')
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


TWO_YEAR_URL = 'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1901099.html'
TWO_YEAR_TITLE = '2021年4月份规模以上工业增加值增长9.8%两年平均增长6.8%'


def _two_year_industry_report():
    # 2021-04实际主表：同比9.8、累计20.3；6.8只在正文/标题中。
    table = '''<p>2021年4月份规模以上工业生产主要数据</p><table>
    <tr><td rowspan="2"></td><td colspan="2">4月</td><td colspan="2">1—4月</td></tr>
    <tr><td>绝对量</td><td>同比增长（%）</td><td>绝对量</td><td>同比增长（%）</td></tr>
    <tr><td>规模以上工业增加值</td><td>…</td><td>9.8</td><td>…</td><td>20.3</td></tr></table>'''
    body = (f'<meta name="ArticleTitle" content="{TWO_YEAR_TITLE}"><meta name="PubDate" content="2021/05/17 10:00">'
        '<p>4月份，规模以上工业增加值同比实际增长9.8%（以下增加值增速均为扣除价格因素的实际增长率），'
        '比2019年同期增长14.1%，两年平均增长6.8%。从环比看，4月份，规模以上工业增加值比上月增长0.52%。'
        '1—4月份，规模以上工业增加值同比增长20.3%。</p>' + table)
    return body, table


@pytest.mark.parametrize('mobile_copy', [False, True])
def test_two_year_industry_returns_monthly_yoy_not_average_or_cumulative(mobile_copy):
    body, table = _two_year_industry_report()
    release = history.NbsReleaseLocator(series_id='cn.industry.value_added_yoy', url=TWO_YEAR_URL, title=TWO_YEAR_TITLE)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    row = history.parse_history_release(release, source, body + (table if mobile_copy else ''))
    assert row.observed_at == date(2021, 4, 1)
    assert row.value == Decimal('9.8')
    assert row.released_at.isoformat() == '2021-05-17T02:00:00+00:00'


def test_two_year_industry_discovery_recognizes_full_official_title():
    page = f'<a href="{TWO_YEAR_URL}" title="{TWO_YEAR_TITLE}">report</a><script>createPageHTML(67,0,"index","html");</script>'
    _, releases, _ = history.discover_history_page(page, 0, date(2021, 4, 1), date(2021, 4, 30))
    assert [(r.series_id, r.observed_at, r.title) for r in releases] == [
        ('cn.industry.value_added_yoy', date(2021, 4, 1), TWO_YEAR_TITLE)]


@pytest.mark.parametrize('mutation', [
    'body_month', 'body_year', 'body_average', 'body_yoy', 'base_year', 'missing_actual',
    'missing_deflator', 'missing_average', 'cumulative_tail', 'duplicate_body', 'bad_body',
    'cum_month', 'cum_year', 'cum_value', 'cum_nonfinite', 'missing_cum',
    'caption_year', 'caption_month', 'missing_caption', 'inside_caption',
    'header_month', 'header_basis', 'extra_period_header', 'bad_span',
    'monthly_table', 'cum_table', 'nonfinite_table', 'shifted_label', 'row_width',
])
def test_two_year_industry_rejects_unproven_or_conflicting_evidence(mutation):
    body, table = _two_year_industry_report()
    if mutation in {'body_month', 'body_year', 'cumulative_tail'}:
        prefix = {'body_month': '5月份，', 'body_year': '2020年4月份，', 'cumulative_tail': '1—4月份，'}[mutation]
        body = body.replace('<p>4月份，', '<p>' + prefix)
    elif mutation == 'body_average':
        body = body.replace('两年平均增长6.8%。', '两年平均增长9.8%。')
    elif mutation == 'body_yoy':
        body = body.replace('同比实际增长9.8%', '同比实际增长6.8%')
    elif mutation == 'base_year':
        body = body.replace('比2019年同期', '比2018年同期')
    elif mutation == 'missing_actual':
        body = body.replace('同比实际', '同比')
    elif mutation == 'missing_deflator':
        body = body.replace('（以下增加值增速均为扣除价格因素的实际增长率）', '')
    elif mutation == 'missing_average':
        body = body.replace('，两年平均增长6.8%。', '。')
    elif mutation in {'duplicate_body', 'bad_body'}:
        body += '<p>2021年4月份，规模以上工业增加值同比实际增长' + ('7.7%' if mutation == 'duplicate_body' else 'NaN%') + '。</p>'
    elif mutation in {'cum_month', 'cum_year', 'cum_value', 'cum_nonfinite', 'missing_cum'}:
        replacement = {'cum_month': '1—5月份，规模以上工业增加值同比增长20.3%。',
            'cum_year': '2020年1—4月份，规模以上工业增加值同比增长20.3%。',
            'cum_value': '1—4月份，规模以上工业增加值同比增长9.8%。',
            'cum_nonfinite': '1—4月份，规模以上工业增加值同比增长NaN%。', 'missing_cum': ''}[mutation]
        body = body.replace('1—4月份，规模以上工业增加值同比增长20.3%。', replacement)
    else:
        broken = table
        if mutation == 'caption_year':
            broken = broken.replace('2021年4月份', '2020年4月份')
        elif mutation == 'caption_month':
            broken = broken.replace('2021年4月份', '2021年13月份')
        elif mutation == 'missing_caption':
            broken = broken[broken.index('<table>'):]
        elif mutation == 'inside_caption':
            broken = broken.replace('<table>', '<table><caption>2020年4月份规模以上工业生产主要数据</caption>')
        elif mutation == 'header_month':
            broken = broken.replace('1—4月', '1—5月')
        elif mutation == 'header_basis':
            broken = broken.replace('同比增长', '两年平均增长')
        elif mutation == 'extra_period_header':
            broken = broken.replace('<tr><td>规模以上工业增加值', '<tr><td></td><td>5月</td><td>5月</td><td>1—5月</td><td>1—5月</td></tr><tr><td>规模以上工业增加值')
        elif mutation == 'bad_span':
            broken = broken.replace('rowspan="2"', 'rowspan="3"')
        elif mutation in {'monthly_table', 'nonfinite_table'}:
            broken = broken.replace('<td>9.8</td>', '<td>' + ('6.8' if mutation == 'monthly_table' else 'NaN') + '</td>')
        elif mutation == 'cum_table':
            broken = broken.replace('<td>20.3</td>', '<td>9.8</td>')
        elif mutation == 'shifted_label':
            broken = broken.replace('<td>规模以上工业增加值', '<td></td><td>规模以上工业增加值')
        else:
            broken = broken.replace('</tr></table>', '<td></td></tr></table>')
        body += broken  # 好桌面副本不能掩盖坏移动副本。
    release = history.NbsReleaseLocator(series_id='cn.industry.value_added_yoy', url=TWO_YEAR_URL, title=TWO_YEAR_TITLE)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('replacement', ['增长99.9%', '下降14.1%'])
def test_two_year_industry_review_rejects_conflicting_signed_base_comparison(replacement):
    body, _ = _two_year_industry_report()
    paragraph = body[body.index('<p>4月份，'):body.index('</p>') + 4]
    body += paragraph.replace('比2019年同期增长14.1%', '比2019年同期' + replacement)
    release = history.NbsReleaseLocator(series_id='cn.industry.value_added_yoy', url=TWO_YEAR_URL, title=TWO_YEAR_TITLE)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('prefix', ['3—4月份', '3-4月份', '3–4月份', '12020年4月份', '2020年3—4月份'])
@pytest.mark.parametrize('cumulative', [False, True])
def test_two_year_industry_review_rejects_hidden_invalid_year_or_interval_evidence(prefix, cumulative):
    body, _ = _two_year_industry_report()
    if cumulative:
        body += f'<p>{prefix}，规模以上工业增加值同比增长7.7%。</p>'
    else:
        paragraph = body[body.index('<p>4月份，'):body.index('</p>') + 4]
        body += paragraph.replace('<p>4月份，', f'<p>{prefix}，').replace('同比实际增长9.8%', '同比实际增长7.7%')
    release = history.NbsReleaseLocator(series_id='cn.industry.value_added_yoy', url=TWO_YEAR_URL, title=TWO_YEAR_TITLE)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('basis', ['比2019年同期增长（%）', '两年平均增长（%）', '一季度', '第五季度'])
def test_two_year_industry_review_rejects_additional_base_or_period_header(basis):
    body, table = _two_year_industry_report()
    extra = f'<tr><td>指标</td><td>绝对量</td><td>{basis}</td><td>绝对量</td><td>{basis}</td></tr>'
    body += table.replace('<tr><td>规模以上工业增加值', extra + '<tr><td>规模以上工业增加值')
    release = history.NbsReleaseLocator(series_id='cn.industry.value_added_yoy', url=TWO_YEAR_URL, title=TWO_YEAR_TITLE)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


def _two_year_cumulative_report(month):
    title, report_id, published, monthly, base, average, cumulative = {
        5: ('2021年5月份规模以上工业增加值增长8.8%两年平均增长6.6%', '1901126', '2021/06/16 15:00', '8.8', '13.6', '6.6', '17.8'),
        6: ('2021年6月份规模以上工业增加值增长8.3%两年平均增长6.5%', '1901155', '2021/07/15 10:00', '8.3', '13.5', '6.5', '15.9'),
    }[month]
    table = f'''<p>2021年{month}月份规模以上工业生产主要数据</p><table>
    <tr><td rowspan="2"></td><td colspan="2">{month}月</td><td colspan="2">1—{month}月</td></tr>
    <tr><td>绝对量</td><td>同比增长（%）</td><td>绝对量</td><td>同比增长（%）</td></tr>
    <tr><td>规模以上工业增加值</td><td>…</td><td>{monthly}</td><td>…</td><td>{cumulative}</td></tr></table>'''
    body = (f'<meta name="ArticleTitle" content="{title}"><meta name="PubDate" content="{published}">'
        f'<p>{month}月份，规模以上工业增加值同比实际增长{monthly}%（以下增加值增速均为扣除价格因素的实际增长率），'
        f'比2019年同期增长{base}%，两年平均增长{average}%。'
        f'1—{month}月份，规模以上工业增加值同比增长{cumulative}%，两年平均增长7.0%。</p>' + table)
    release = history.NbsReleaseLocator(series_id='cn.industry.value_added_yoy',
        url=f'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_{report_id}.html', title=title)
    return release, body, table


@pytest.mark.parametrize(('month', 'value', 'released'), [(5, '8.8', '2021-06-16T07:00:00+00:00'), (6, '8.3', '2021-07-15T02:00:00+00:00')])
@pytest.mark.parametrize('mobile_copy', [False, True])
def test_two_year_cumulative_average_returns_single_month_not_any_average(month, value, released, mobile_copy):
    release, body, table = _two_year_cumulative_report(month)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    row = history.parse_history_release(release, source, body + (table if mobile_copy else ''))
    assert row.observed_at == date(2021, month, 1)
    assert row.value == Decimal(value)
    assert row.released_at.isoformat() == released


@pytest.mark.parametrize('month', [5, 6])
@pytest.mark.parametrize('mutation', ['average_value', 'average_direction', 'average_nonfinite', 'average_missing_number',
    'extra_basis', 'wrong_year', 'wrong_interval', 'quarter_prefix', 'duplicate_missing_average'])
def test_two_year_cumulative_average_rejects_inconsistent_or_unknown_full_cumulative_evidence(month, mutation):
    release, body, _ = _two_year_cumulative_report(month)
    cumulative_value = '17.8' if month == 5 else '15.9'
    paragraph = f'<p>1—{month}月份，规模以上工业增加值同比增长{cumulative_value}%，两年平均增长7.0%。</p>'
    if mutation == 'average_value':
        paragraph = paragraph.replace('7.0%', '99.9%')
    elif mutation == 'average_direction':
        paragraph = paragraph.replace('平均增长7.0%', '平均下降7.0%')
    elif mutation == 'average_nonfinite':
        paragraph = paragraph.replace('7.0%', 'NaN%')
    elif mutation == 'average_missing_number':
        paragraph = paragraph.replace('平均增长7.0%', '平均增长')
    elif mutation == 'extra_basis':
        paragraph = paragraph.replace('平均增长7.0%', '平均增长7.0%，环比增长0.52%')
    elif mutation == 'wrong_year':
        paragraph = paragraph.replace('<p>', '<p>2020年')
    elif mutation == 'wrong_interval':
        paragraph = paragraph.replace(f'1—{month}月份', f'2—{month}月份')
    elif mutation == 'quarter_prefix':
        paragraph = paragraph.replace(f'1—{month}月份', '上半年')
    else:
        paragraph = paragraph.replace('，两年平均增长7.0%', '')
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body + paragraph)


@pytest.mark.parametrize('month', [5, 6])
@pytest.mark.parametrize('period', ['2021年', '2020年', '半年', '2021年度'])
def test_two_year_cumulative_review_rejects_unknown_annual_or_half_year_evidence(month, period):
    release, body, _ = _two_year_cumulative_report(month)
    body += f'<p>{period}，规模以上工业增加值同比增长17.8%，两年平均增长NaN%。</p>'
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


def _legacy_sep_or_half_cpi_report(kind):
    # 独立手核对的两种真实布局，不使用生产表头推导期望值。
    if kind == 'sep':
        identity = {'seriesId': 'cn.cpi.core_yoy',
            'url': 'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1899681.html',
            'title': '2017年9月份居民消费价格同比上涨1.6%'}
        year, month, published, headline = 2017, 9, '2017/10/16 09:30', '1.6'
        table = '''<p>2017年9月居民消费价格主要数据</p><table>
        <tr><td rowspan="2"></td><td colspan="2">9月</td><td>1-9月</td></tr>
        <tr><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>
        <tr><td>居民消费价格</td><td>0.5</td><td>1.6</td><td>1.5</td></tr>
        <tr><td>其中：不包括食品和能源</td><td>0.5</td><td>2.3</td><td>2.1</td></tr></table>'''
    else:
        identity = {'seriesId': 'cn.cpi.core_yoy',
            'url': 'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_1899983.html',
            'title': '2018年6月份居民消费价格同比上涨1.9%'}
        year, month, published, headline = 2018, 6, '2018/07/10 09:30', '1.9'
        table = '''<table><tr><td colspan="4">2018年6月居民消费价格主要数据</td></tr>
        <tr><td rowspan="2"></td><td rowspan="2">环比涨跌幅（%）</td><td rowspan="2">同比涨跌幅（%）</td><td>上半年</td></tr>
        <tr><td>同比涨跌幅（%）</td></tr>
        <tr><td>居民消费价格</td><td>-0.1</td><td>1.9</td><td>2.0</td></tr>
        <tr><td>其中：不包括食品和能源</td><td>0.1</td><td>1.9</td><td>2.0</td></tr></table>'''
    body = (f'<meta name="ArticleTitle" content="{identity["title"]}"><meta name="PubDate" content="{published}">'
            f'<p>{year}年{month}月份，全国居民消费价格同比上涨{headline}%。</p>' + table)
    return identity, body, table


@pytest.mark.parametrize(('kind', 'value', 'observed', 'released'), [
    ('sep', '2.3', date(2017, 9, 1), '2017-10-16T01:30:00+00:00'),
    ('half', '1.9', date(2018, 6, 1), '2018-07-10T01:30:00+00:00')])
@pytest.mark.parametrize('mobile_copy', [False, True])
def test_legacy_sep_and_half_cpi_returns_core_month_yoy(kind, value, observed, released, mobile_copy):
    # 拒绝分派、错取累计/环比/headline或猜发布日期都会使本测试失败。
    identity, body, table = _legacy_sep_or_half_cpi_report(kind)
    if mobile_copy:
        body += table
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    row = history.parse_history_release(release, source, body)
    assert row.value == Decimal(value)
    assert row.observed_at == observed
    assert row.released_at.isoformat() == released


@pytest.mark.parametrize('mobile_copy', [False, True])
def test_half_cpi_uses_core_row_even_when_different_from_total_cpi(mobile_copy):
    # 官方6月核心值恰好等于总CPI；受控不同核心值排除headline替代的假阳性。
    identity, body, table = _legacy_sep_or_half_cpi_report('half')
    original = '<td>其中：不包括食品和能源</td><td>0.1</td><td>1.9</td><td>2.0</td>'
    changed = original.replace('<td>1.9</td>', '<td>2.7</td>')
    body = body.replace(original, changed)
    if mobile_copy:
        body += table.replace(original, changed)
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    assert history.parse_history_release(release, source, body).value == Decimal('2.7')


@pytest.mark.parametrize('kind', ['sep', 'half'])
@pytest.mark.parametrize('mutation', ['caption_year', 'caption_month', 'missing_caption', 'inside_caption',
    'wrong_period', 'unknown_header', 'extra_period', 'missing_total', 'wrong_total',
    'core_nonfinite', 'total_nonfinite', 'row_width', 'shifted_core', 'row_span',
    'header_row_span', 'header_col_span', 'copy_core_mom', 'copy_core_yoy',
    'copy_core_cumulative', 'copy_total_mom', 'copy_total_cumulative', 'missing_core'])
def test_legacy_sep_and_half_cpi_rejects_unproven_or_conflicting_evidence(kind, mutation):
    identity, body, table = _legacy_sep_or_half_cpi_report(kind)
    year, month = (2017, 9) if kind == 'sep' else (2018, 6)
    core = ('<td>0.5</td><td>2.3</td><td>2.1</td>' if kind == 'sep'
            else '<td>0.1</td><td>1.9</td><td>2.0</td>')
    total = ('<td>0.5</td><td>1.6</td><td>1.5</td>' if kind == 'sep'
             else '<td>-0.1</td><td>1.9</td><td>2.0</td>')
    mobile = table
    if mutation == 'caption_year':
        mobile = mobile.replace(f'{year}年{month}月', f'{year - 1}年{month}月')
    elif mutation == 'caption_month':
        mobile = mobile.replace(f'{year}年{month}月', f'{year}年13月')
    elif mutation == 'missing_caption':
        caption = (f'<p>{year}年{month}月居民消费价格主要数据</p>' if kind == 'sep'
                   else f'<tr><td colspan="4">{year}年{month}月居民消费价格主要数据</td></tr>')
        mobile = mobile.replace(caption, '')
    elif mutation == 'inside_caption':
        mobile = mobile.replace('<table>', f'<table><caption>{year - 1}年{month}月居民消费价格主要数据</caption>')
    elif mutation == 'wrong_period':
        mobile = mobile.replace('1-9月', '1-8月') if kind == 'sep' else mobile.replace('上半年', '下半年')
    elif mutation == 'unknown_header':
        mobile = mobile.replace('环比涨跌幅', '未知涨跌幅')
    elif mutation == 'extra_period':
        mobile = mobile.replace('<tr><td>居民消费价格',
            '<tr><td></td><td>13月</td><td>第五季度</td><td>全年</td></tr><tr><td>居民消费价格')
    elif mutation == 'missing_total':
        mobile = mobile.replace('<td>居民消费价格</td>', '<td>城市</td>')
    elif mutation == 'wrong_total':
        cells = total.split('</td>')[:-1]
        cells[1] = '<td>99.9'
        mobile = mobile.replace(total, '</td>'.join(cells) + '</td>')
    elif mutation in {'core_nonfinite', 'total_nonfinite'}:
        original = core if mutation == 'core_nonfinite' else total
        cells = original.split('</td>')[:-1]
        cells[2] = '<td>NaN'
        mobile = mobile.replace(original, '</td>'.join(cells) + '</td>')
    elif mutation == 'row_width':
        mobile = mobile.replace('</tr></table>', '<td></td></tr></table>')
    elif mutation == 'shifted_core':
        mobile = mobile.replace('<td>其中：不包括食品和能源</td>', '<td></td><td>其中：不包括食品和能源</td>')
    elif mutation == 'row_span':
        mobile = mobile.replace('<td>其中：不包括食品和能源</td>', '<td colspan="2">其中：不包括食品和能源</td>')
    elif mutation == 'header_row_span':
        mobile = mobile.replace('rowspan="2"', 'rowspan="3"')
    elif mutation == 'header_col_span':
        mobile = mobile.replace('colspan="2"', 'colspan="1"') if kind == 'sep' else mobile.replace('colspan="4"', 'colspan="3"')
    elif mutation.startswith('copy_'):
        original = core if mutation.startswith('copy_core') else total
        column = {'copy_core_mom': 0, 'copy_core_yoy': 1, 'copy_core_cumulative': 2,
                  'copy_total_mom': 0, 'copy_total_cumulative': 2}[mutation]
        cells = original.split('</td>')[:-1]
        cells[column] = '<td>99.9'
        mobile = mobile.replace(original, '</td>'.join(cells) + '</td>')
    else:
        mobile = mobile.replace('其中：不包括食品和能源', '其中：非食品')
        body = body.replace(table, mobile)  # 单表也必须有核心行，不借另一好表证明。
    if mutation != 'missing_core':
        body += mobile
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


REMAINING_CPI_PERIODS = ['2017-06', '2017-07', '2017-08', '2017-10', '2017-11', '2018-02', '2018-04']


def _remaining_legacy_cpi_report(period):
    # 独立核对正式页的字面数值/元数据；不导入生产模板或从headline计算核心值。
    report_id, headline, published, core, total, layout = {
        '2017-06': ('1899568', '1.5', '2017/07/10 09:30', ('0.1', '2.2', '2.1'), ('-0.2', '1.5', '1.4'), 'half2017'),
        '2017-07': ('1899609', '1.4', '2017/08/09 09:30', ('0.3', '2.1', '2.1'), ('0.1', '1.4', '1.4'), 'dual'),
        '2017-08': ('1899643', '1.8', '2017/09/09 09:30', ('0.1', '2.2', '2.1'), ('0.4', '1.8', '1.5'), 'dual'),
        '2017-10': ('1899722', '1.9', '2017/11/09 09:30', ('0.0', '2.3', '2.1'), ('0.1', '1.9', '1.5'), 'dual'),
        '2017-11': ('1899756', '1.7', '2017/12/09 09:30', ('0.0', '2.3', '2.2'), ('0.0', '1.7', '1.5'), 'dual'),
        '2018-02': ('1899858', '2.9', '2018/03/09 09:30', ('0.5', '2.5', '2.2'), ('1.2', '2.9', '2.2'), 'dual'),
        '2018-04': ('1899929', '1.8', '2018/05/10 09:30', ('0.2', '2.0', '2.1'), ('-0.2', '1.8', '2.1'), 'stacked'),
    }[period]
    year, month = map(int, period.split('-'))
    caption = f'{year}年{month}月居民消费价格主要数据'
    if layout == 'dual':
        prefix = f'<p>{caption}</p><table>'
        headers = (f'<tr><td rowspan="2"></td><td colspan="2">{month}月</td><td>1-{month}月</td></tr>'
            '<tr><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>')
    elif layout == 'half2017':
        prefix = f'<table><tr><td colspan="4">{caption}</td></tr>'
        headers = ('<tr><td rowspan="2"></td><td colspan="2">6月</td><td>上半年</td></tr>'
            '<tr><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td><td>涨跌幅（%）</td></tr>')
    else:
        prefix = f'<table><tr><td colspan="4">{caption}</td></tr>'
        headers = ('<tr><td rowspan="2"></td><td rowspan="2">环比涨跌幅（%）</td>'
            '<td rowspan="2">同比涨跌幅（%）</td><td>1-4月</td></tr><tr><td>同比涨跌幅（%）</td></tr>')
    total_row = '<tr><td>居民消费价格</td>' + ''.join(f'<td>{value}</td>' for value in total) + '</tr>'
    core_row = '<tr><td>其中：不包括食品和能源</td>' + ''.join(f'<td>{value}</td>' for value in core) + '</tr>'
    table = prefix + headers + total_row + core_row + '</table>'
    title = f'{year}年{month}月份居民消费价格同比上涨{headline}%'
    identity = {'seriesId': 'cn.cpi.core_yoy',
        'url': f'https://www.stats.gov.cn/sj/zxfb/202302/t20230203_{report_id}.html', 'title': title}
    body = (f'<meta name="ArticleTitle" content="{title}"><meta name="PubDate" content="{published}">'
        f'<p>{year}年{month}月份，全国居民消费价格同比上涨{headline}%。</p>' + table)
    return identity, body, table, core_row, total_row


@pytest.mark.parametrize(('period', 'value', 'released'), [
    ('2017-06', '2.2', '2017-07-10T01:30:00+00:00'),
    ('2017-07', '2.1', '2017-08-09T01:30:00+00:00'),
    ('2017-08', '2.2', '2017-09-09T01:30:00+00:00'),
    ('2017-10', '2.3', '2017-11-09T01:30:00+00:00'),
    ('2017-11', '2.3', '2017-12-09T01:30:00+00:00'),
    ('2018-02', '2.5', '2018-03-09T01:30:00+00:00'),
    ('2018-04', '2.0', '2018-05-10T01:30:00+00:00')])
@pytest.mark.parametrize('mobile_copy', [False, True])
def test_remaining_legacy_cpi_returns_core_month_yoy(period, value, released, mobile_copy):
    # 去掉限定分派、错取环比/累计/总CPI或猜日期都会使这里失败。
    identity, body, table, _, _ = _remaining_legacy_cpi_report(period)
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    row = history.parse_history_release(release, source, body + (table if mobile_copy else ''))
    assert row.value == Decimal(value)
    assert row.observed_at.isoformat() == period + '-01'
    assert row.released_at.isoformat() == released


@pytest.mark.parametrize('period', REMAINING_CPI_PERIODS)
@pytest.mark.parametrize('mutation', ['caption_year', 'caption_month', 'missing_caption', 'inside_caption',
    'quarter_caption', 'half_caption', 'annual_caption', 'wrong_period', 'unknown_header',
    'extra_basis', 'extra_period', 'missing_total', 'wrong_total', 'core_nonfinite', 'total_nonfinite',
    'row_width', 'shifted_core', 'core_span', 'zero_core_span', 'header_row_span', 'header_col_span',
    'copy_core_mom', 'copy_core_yoy', 'copy_core_cumulative', 'copy_total_mom', 'copy_total_cumulative',
    'other_row_span', 'missing_core'])
def test_remaining_legacy_cpi_rejects_unproven_or_conflicting_evidence(period, mutation):
    identity, body, table, core, total = _remaining_legacy_cpi_report(period)
    year, month = map(int, period.split('-'))
    caption = f'{year}年{month}月居民消费价格主要数据'
    mobile = table
    if mutation in {'caption_year', 'caption_month', 'quarter_caption', 'half_caption', 'annual_caption'}:
        changed = {'caption_year': f'{year - 1}年{month}月', 'caption_month': f'{year}年13月',
            'quarter_caption': f'{year}年一季度', 'half_caption': f'{year}年上半年',
            'annual_caption': f'{year}年'}[mutation] + '居民消费价格主要数据'
        if mutation.startswith('caption_'):
            mobile = mobile.replace(caption, changed)
        else:
            mobile = mobile.replace('<table>', f'<table><caption>{changed}</caption>')
    elif mutation == 'missing_caption':
        if '<p>' in mobile:
            mobile = mobile.replace(f'<p>{caption}</p>', '')
        else:
            mobile = mobile.replace(f'<tr><td colspan="4">{caption}</td></tr>', '')
    elif mutation == 'inside_caption':
        mobile = mobile.replace('<table>', f'<table><caption>{year - 1}年{month}月居民消费价格主要数据</caption>')
    elif mutation == 'wrong_period':
        mobile = mobile.replace('上半年', '下半年').replace(f'1-{month}月', '1-13月')
    elif mutation == 'unknown_header':
        mobile = mobile.replace('环比涨跌幅', '未知涨跌幅')
    elif mutation in {'extra_basis', 'extra_period'}:
        basis = '同比涨跌幅（%）' if mutation == 'extra_basis' else f'{year}年度'
        mobile = mobile.replace('<tr><td>居民消费价格', f'<tr><td>{basis}</td><td></td><td></td><td></td></tr><tr><td>居民消费价格')
    elif mutation == 'missing_total':
        mobile = mobile.replace('<td>居民消费价格</td>', '<td>城市</td>')
    elif mutation == 'wrong_total':
        cells = total.split('</td>')
        cells[2] = '<td>99.9'
        mobile = mobile.replace(total, '</td>'.join(cells))
    elif mutation in {'core_nonfinite', 'total_nonfinite'} or mutation.startswith('copy_'):
        original = core if mutation == 'core_nonfinite' or mutation.startswith('copy_core') else total
        column = (3 if mutation.endswith('nonfinite') else
            {'mom': 1, 'yoy': 2, 'cumulative': 3}[mutation.rsplit('_', 1)[-1]])
        cells = original.split('</td>')
        cells[column] = '<td>' + ('NaN' if mutation.endswith('nonfinite') else '99.9')
        mobile = mobile.replace(original, '</td>'.join(cells))
    elif mutation == 'row_width':
        mobile = mobile.replace('</tr></table>', '<td></td></tr></table>')
    elif mutation == 'shifted_core':
        mobile = mobile.replace('<td>其中：不包括食品和能源', '<td></td><td>其中：不包括食品和能源')
    elif mutation in {'core_span', 'zero_core_span'}:
        mobile = mobile.replace('<td>其中：不包括食品和能源', '<td colspan="' + ('0' if mutation == 'zero_core_span' else '2') + '">其中：不包括食品和能源')
    elif mutation == 'header_row_span':
        mobile = mobile.replace('rowspan="2"', 'rowspan="3"')
    elif mutation == 'header_col_span':
        mobile = mobile.replace('colspan="2"', 'colspan="3"').replace('colspan="4"', 'colspan="3"')
    elif mutation == 'other_row_span':
        mobile = mobile.replace(core, '<tr><td rowspan="2">食品</td><td>0.1</td><td>0.2</td><td>0.3</td></tr>' + core)
    else:
        mobile = mobile.replace('其中：不包括食品和能源', '其中：非食品')
        body = body.replace(table, mobile)  # 不借好表伪造一个失去核心身份的副本。
    if mutation != 'missing_core':
        body += mobile
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('period', REMAINING_CPI_PERIODS)
def test_remaining_legacy_cpi_does_not_generalize_to_unverified_year(period):
    identity, body, _, _, _ = _remaining_legacy_cpi_report(period)
    original_year = period[:4]
    identity['title'] = identity['title'].replace(original_year + '年', '2019年')
    body = body.replace(original_year + '年', '2019年').replace('2017/', '2020/').replace('2018/', '2020/')
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('period', REMAINING_CPI_PERIODS)
@pytest.mark.parametrize('conflicting_total', [False, True])
def test_remaining_cpi_review_rejects_related_copy_without_core(period, conflicting_total):
    identity, body, table, core, total = _remaining_legacy_cpi_report(period)
    mobile = table.replace(core, '')
    if conflicting_total:
        cells = total.split('</td>')
        cells[2] = '<td>99.9'
        mobile = mobile.replace(total, '</td>'.join(cells))
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body + mobile)


@pytest.mark.parametrize('period', REMAINING_CPI_PERIODS)
@pytest.mark.parametrize('hidden', ['other_row', 'total_row'])
def test_remaining_cpi_review_rejects_whole_hidden_raw_row(period, hidden):
    identity, body, _, core, _ = _remaining_legacy_cpi_report(period)
    if hidden == 'other_row':
        row = '<tr><td rowspan="2" colspan="0">食品</td></tr>'
    else:
        row = '<tr><td colspan="0">居民消费价格</td><td colspan="0">99.7</td><td colspan="0">99.9</td><td colspan="0">99.8</td></tr>'
    body = body.replace(core, row + core)
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body)


@pytest.mark.parametrize('period', REMAINING_CPI_PERIODS)
@pytest.mark.parametrize('caption', ['上半年居民消费价格主要数据', '居民消费价格主要数据',
    '下半年居民消费价格主要数据', '一季度居民消费价格主要数据', 'monthly_cumulative_suffix'])
def test_remaining_cpi_review_rejects_complete_unknown_caption(period, caption):
    identity, body, table, _, _ = _remaining_legacy_cpi_report(period)
    year, month = map(int, period.split('-'))
    if caption == 'monthly_cumulative_suffix':
        caption = f'{year}年{month}月居民消费价格主要数据（累计口径）'
    mobile = table.replace('<table>', f'<table><caption>{caption}</caption>')
    release = history.NbsReleaseLocator.model_validate(identity)
    source = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE).require(release.series_id).sources[0]
    with pytest.raises(ProviderSchemaError):
        history.parse_history_release(release, source, body + mobile)
