"""银行ETF已核实的公共网页合同；签名常量为公开协议，非用户密钥。"""
from datetime import date,datetime,timezone
from decimal import Decimal
from hashlib import md5,sha256
import re
import httpx

from app.macro.extensions.client import decode_json
from app.macro.extensions.store import instant
from app.macro.issuer_html_contracts import parse_issuer_html_metadata

FUND_CODE='512800'
INDEX_CODE='399986'
INDEX_NAME='中证银行指数'
METADATA_URL='https://www.fsfund.com/fund/512800/fundDetail.shtml'
PCF_URL='https://api.fsfund.com/v2/webzk/queryController/getFundShareInfo'
# 官方common.js原件SHA256：本值只实现普通公开查询，不读取账户、Cookie或根.env凭据。
PUBLIC_PROTOCOL_SHA256='560b54f91a7e1fb76ddc17d8a8c07d02f12166fd83118cb96d3b42c97615a00a'
_PUBLIC_WEB_CONSTANT='CD364559FDA24D53B05F01E943ECDFCC'


def fsfund_request_payload(fund_code,trading_date,now=None):
    if (fund_code!=FUND_CODE or type(trading_date) is not str or date.fromisoformat(trading_date).isoformat()!=trading_date):
        raise ValueError('FSFUND_QUERY_INVALID')
    at=instant(datetime.now(timezone.utc) if now is None else now)
    value={'fundCode':fund_code,'startDate':trading_date.replace('-',''),'netNo':'web','timestamp':str(int(at.timestamp()*1000))}
    signed=''.join(k+'='+value[k]+'&' for k in sorted(value))+'key='+_PUBLIC_WEB_CONSTANT
    value['signature']=md5(signed.encode('utf8')).hexdigest()
    return value


class PublicBankTransport(httpx.AsyncBaseTransport):
    def __init__(self,inner=None):self.inner=inner if inner is not None else httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self,request):
        if request.method!='POST' or str(request.url)!=PCF_URL:raise ValueError('FSFUND_TRANSPORT_SCOPE_INVALID')
        request.headers['netNo']='web'
        return await self.inner.handle_async_request(request)

    async def aclose(self):await self.inner.aclose()


def official_transport(document,transport):
    if document['fundCode']==FUND_CODE and document['kind']=='fund-pcf':return PublicBankTransport(transport)
    return transport


def parse_fsfund_contract(body,fund_code,expected_index_name):
    try:
        if fund_code!=FUND_CODE or expected_index_name!=INDEX_NAME:raise ValueError()
        value=parse_issuer_html_metadata(body,fund_code)
        if value['trackingIndexName']!=INDEX_NAME:raise ValueError()
        return {k:value[k] for k in ('schemaVersion','fundCode','fundName','trackingIndexName','rawSha256')}|{'downloadResourceKey':FUND_CODE}
    except Exception:raise ValueError('FUND_CONTRACT_DISCOVERY_RESPONSE_INVALID') from None


def parse_fsfund_pcf(body,fund_code,trading_date,*,max_members=10000):
    try:
        if (type(body) is not bytes or not 1<=len(body)<=8388608 or fund_code!=FUND_CODE
            or type(trading_date) is not str or date.fromisoformat(trading_date).isoformat()!=trading_date
            or type(max_members) is not int or not 1<=max_members<=10000):raise ValueError()
        value=decode_json(body)
        if (type(value) is not dict or set(value)!={'code','message','error','success','data'} or value['code']!='0000'
            or value['error'] is not False or value['success'] is not True):raise ValueError()
        rows=value['data']
        if type(rows) is not list or not 1<=len(rows)<=max_members:raise ValueError()
        members=set();qualified=[];markets={'101':'SH','102':'SZ','106':'BJ'}
        for row in rows:
            if (type(row) is not dict or row.get('fundId')!=fund_code or row.get('tradingDay')!=trading_date.replace('-','')
                or type(row.get('stockCode')) is not str or re.fullmatch('[0-9]{6}',row['stockCode']) is None
                or row['stockCode'] in members or type(row.get('stockShort')) is not str or not row['stockShort'].strip()
                or type(row.get('scid')) is not str or row['scid'] not in markets
                or row.get('tdbz') not in ('允许','必须','禁止') or type(row.get('number')) not in (int,str)):raise ValueError()
            number=str(row['number'])
            if not 1<=len(number)<=40 or re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',number) is None:raise ValueError()
            quantity=Decimal(number)
            if not quantity.is_finite() or not 0<=quantity<Decimal('1e18'):raise ValueError()
            members.add(row['stockCode']);qualified.append(row['stockCode']+'.'+markets[row['scid']])
        return {'schemaVersion':'1.0','fundCode':fund_code,'asOfDate':trading_date,'memberCodes':sorted(members),
            'qualifiedMemberCodes':sorted(qualified),'memberCount':len(members),
            'membershipBasis':'published-pcf-all-components-not-fund-holdings','rawSha256':sha256(body).hexdigest()}
    except Exception:raise ValueError('FSFUND_PCF_INVALID') from None
