from importlib import import_module

import pytest


def body(code='512880',day='20261023',records='2',components=None):
    components=components if components is not None else '<Component><InstrumentID>600000</InstrumentID><Quantity>0</Quantity><SubstitutionFlag>2</SubstitutionFlag></Component><Component><InstrumentID>000001</InstrumentID><Quantity>5300</Quantity><SubstitutionFlag>1</SubstitutionFlag></Component>'
    return f'<?xml version="1.0" encoding="UTF-8"?><SSEPortfolioCompositionFile><FundInstrumentID>{code}</FundInstrumentID><TradingDay>{day}</TradingDay><RecordNumber>{records}</RecordNumber><ComponentList>{components}</ComponentList></SSEPortfolioCompositionFile>'.encode()


def parse(value=None,**kw):
    return import_module('app.macro.official_members').parse_fund_pcf(body() if value is None else value,'512880','2026-10-23',**kw)


def test_pcf_fund_date_and_complete_components_are_independent_evidence():
    value=parse()
    assert value['fundCode']=='512880' and value['asOfDate']=='2026-10-23'
    assert value['memberCodes']==['000001','600000'] and value['memberCount']==2
    assert value['membershipBasis']=='published-pcf-all-components-not-fund-holdings'
    assert len(value['rawSha256'])==64  # 零数量/现金替代成员不被丢掉。


@pytest.mark.parametrize('bad',[
    body(code='512881'),body(day='20261022'),body(day='20260230'),body(records='3'),
    body(records='1'),body(records='0',components=''),body(records='01'),
    body().replace(b'<Quantity>0</Quantity>',b'<Quantity>-1</Quantity>'),
    body().replace(b'<Quantity>0</Quantity>',b'<Quantity>NaN</Quantity>'),
    body().replace(b'<InstrumentID>000001</InstrumentID>',b'<InstrumentID>600000</InstrumentID>'),
    body().replace(b'<InstrumentID>600000</InstrumentID>',b'<InstrumentID>600000.SH</InstrumentID>'),
    body().replace(b'<TradingDay>',b'<TradingDay>20261023</TradingDay><TradingDay>',1),
    body().replace(b'<Quantity>',b'<Quantity>0</Quantity><Quantity>',1),
    body().replace(b'<SSEPortfolioCompositionFile>',b'<SSEPortfolioCompositionFile xmlns="unknown">'),
    body().replace(b'<SSEPortfolioCompositionFile>',b'<!DOCTYPE x [<!ENTITY a "512880">]><SSEPortfolioCompositionFile>'),
    body().replace(b'<FundInstrumentID>512880</FundInstrumentID>',b'<FundInstrumentID>&a;</FundInstrumentID>'),
    b'<html>200 error</html>',body()[:-10],b'\xff',
])
def test_wrong_fund_dates_count_duplicates_and_unsafe_xml_rejected(bad):
    with pytest.raises(ValueError):parse(bad)


def test_member_limit_and_exact_expected_date_required():
    with pytest.raises(ValueError):parse(max_members=1)
    with pytest.raises(ValueError):import_module('app.macro.official_members').parse_fund_pcf(body(),'512880','2026-02-30')
