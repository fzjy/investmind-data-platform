from importlib import import_module

import pytest

from backend.tests.macro.official_xls_fixture import workbook,compound_workbook,HEADERS


def parse(body=None,index='000300'):
    return import_module('app.macro.official_members').parse_csi_members(workbook() if body is None else body,index)


def row(**fields):
    value=dict(date='20261023',index='000300',indexName='沪深300',indexEnglish='CSI 300',
        code='600000',name='浦发银行',english='SPD BANK',exchange='上海证券交易所',exchangeEnglish='Shanghai Stock Exchange')
    value.update(fields);return list(value.values())


def test_real_biff_bytes_are_parsed_with_independent_identity_and_date():
    result=parse()
    assert result['indexCode']=='000300' and result['asOfDate']=='2026-10-23'
    assert result['members']==['000001.SZ','600000.SH'] and result['memberCount']==2
    assert len(result['rawSha256'])==64 and result['indexName']=='沪深300'


def test_ole_container_with_real_biff_workbook_is_supported():
    assert parse(compound_workbook())['members']==['000001.SZ','600000.SH']


def test_official_bilingual_headers_can_be_joined_without_a_line_break():
    headers=[h.replace('\n','') for h in HEADERS]
    assert parse(workbook(headers=headers))['memberCount']==2
    headers[4]='成份券代码Index Code'
    with pytest.raises(ValueError):parse(workbook(headers=headers))


def test_numeric_excel_identity_retains_leading_zero_without_guessing_exchange():
    assert parse(workbook([row(index=300,code=1,exchange='深圳证券交易所',exchangeEnglish='Shenzhen Stock Exchange')]))['members']==['000001.SZ']


@pytest.mark.parametrize('fields',[
    {'date':'20260230'}, {'date':'2026-10-23'}, {'date':True}, {'index':'000905'},
    {'code':'60000'}, {'code':'600000.SH'}, {'code':1.5}, {'name':''}, {'english':''},
    {'exchange':'未知交易所'}, {'exchangeEnglish':'Shenzhen Stock Exchange'},
])
def test_invalid_or_ambiguous_identity_rejected(fields):
    with pytest.raises(ValueError):parse(workbook([row(**fields)]))


@pytest.mark.parametrize('rows',[
    [],[row(),row()],[row(),row(code='600001',date='20261022')],
    [row(),row(code='600001',indexName='另一个指数')],
    [row(),['']*9],
])
def test_empty_duplicate_mixed_or_incomplete_rows_rejected(rows):
    with pytest.raises(ValueError):parse(workbook(rows))


@pytest.mark.parametrize('body',[b'',b'<html>forbidden</html>',b'PK\x03\x04xlsx',b'\xd0\xcf\x11\xe0'+b'bad',b'\x09\x08\x10\x00'])
def test_non_workbook_and_truncated_documents_rejected(body):
    with pytest.raises(ValueError):parse(body)


def test_semantic_headers_and_member_budget_are_required():
    bad=list(HEADERS);bad[4]='权重(%)'
    with pytest.raises(ValueError):parse(workbook(headers=bad))
    with pytest.raises(ValueError):import_module('app.macro.official_members').parse_csi_members(workbook(),'000300',max_members=1)
