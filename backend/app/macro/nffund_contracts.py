"""南方已核实的两份公开产品合同；不泛化到未知基金或猜下载资源键。"""
from hashlib import sha256
import re
from app.macro.extensions.client import decode_json

METADATA_URL='https://www.nffund.com/nfwebApi/fund/overreview'
CONTRACTS={
    '512400':('000819','中证申万有色金属指数','ETFYSJS','南方中证申万有色金属交易型开放式指数证券投资基金'),
    '512200':('931775','中证全指房地产指数','ETFFDC','南方中证全指房地产交易型开放式指数证券投资基金'),
}


def valid_resource(fund_code,key):
    from app.macro.media_contracts import FUND_CODE as MEDIA_CODE
    if fund_code==MEDIA_CODE:return key==MEDIA_CODE
    from app.macro.etf_definition_contracts import PROFILES as DEFINITION_PROFILES
    if fund_code in DEFINITION_PROFILES:return key==fund_code
    from app.macro.jsfund_contracts import FUND_CODE as CHIP_CODE
    if fund_code==CHIP_CODE:return key==CHIP_CODE
    from app.macro.consumer_xml_contracts import FUND_CODE as CONSUMER_CODE
    if fund_code==CONSUMER_CODE:return key==CONSUMER_CODE
    from app.macro.fsfund_contracts import FUND_CODE
    if fund_code==FUND_CODE:return key==FUND_CODE
    if fund_code in CONTRACTS:return key==CONTRACTS[fund_code][2]
    return type(key) is str and re.fullmatch('[0-9]{6}',key) is not None


def pcf_template(fund_code,key):
    if fund_code not in CONTRACTS or not valid_resource(fund_code,key):raise ValueError('NFFUND_RESOURCE_INVALID')
    return f'https://www.nffund.com/etf/bulletin/{key}/ssepcf_{fund_code}_{{yyyymmdd}}.xml'


def parse_nffund_contract(body,fund_code,expected_index_name):
    try:
        if type(body) is not bytes or not 1<=len(body)<=8388608:raise ValueError()
        index,name,key,full=CONTRACTS[fund_code]
        if expected_index_name!=name:raise ValueError()
        value=decode_json(body)
        if type(value) is not dict or set(value)!={'code','message','data'} or value['code']!='ETS-5BP00000':raise ValueError()
        info=value['data']['fund_info']
        if (type(info) is not dict or info.get('fundCode')!=fund_code or info.get('fundNameEx')!=full
            or info.get('jjglr')!='南方基金管理股份有限公司' or info.get('etfCode')!=key):raise ValueError()
        declarations=[]
        for field in ('tzfw','yjbjjz'):
            text=info.get(field)
            if type(text) is not str or not 1<=len(text)<=20000:raise ValueError()
            declarations+=re.findall(r'本基金(?:的)?标的指数为([^。\n]+)(?:。|$)',text)
        if not declarations or set(declarations)!={name}:raise ValueError()
        return {'schemaVersion':'1.0','fundCode':fund_code,'fundName':full,'trackingIndexName':name,
            'rawSha256':sha256(body).hexdigest(),'downloadResourceKey':key}
    except Exception:raise ValueError('FUND_CONTRACT_DISCOVERY_RESPONSE_INVALID') from None


def request_options(document):
    """只由已验证来源选择协议，金融及既有GT请求的默认协议保持不变。"""
    from app.macro.official_member_policy import OfficialSource
    doc=OfficialSource.model_validate(document)
    from app.macro.media_contracts import FUND_CODE as MEDIA_CODE
    if doc.fundCode==MEDIA_CODE:return 'GET',{'gzip_response_allowed':True}
    from app.macro.etf_definition_contracts import PROFILES as DEFINITION_PROFILES,ev_parameters
    if doc.fundCode in DEFINITION_PROFILES:
        return ('POST',{'form':ev_parameters(doc.tradingDate),'gzip_response_allowed':True}) if doc.fundCode=='515030' else ('GET',{'gzip_response_allowed':True})
    from app.macro.jsfund_contracts import FUND_CODE as CHIP_CODE,request_form
    if doc.fundCode==CHIP_CODE:return 'POST',{'form':request_form(doc.kind,doc.tradingDate),'gzip_response_allowed':True}
    from app.macro.consumer_xml_contracts import FUND_CODE as CONSUMER_CODE
    if doc.fundCode==CONSUMER_CODE:return 'GET',{'gzip_response_allowed':True}
    from app.macro.fsfund_contracts import FUND_CODE,fsfund_request_payload
    if doc.fundCode==FUND_CODE:
        if doc.kind=='fund-mapping':return 'GET',{'gzip_response_allowed':True}
        if doc.kind=='fund-pcf':return 'POST',{'payload':fsfund_request_payload(doc.fundCode,doc.tradingDate),'gzip_response_allowed':True}
    if doc.fundCode in CONTRACTS:
        if doc.kind=='fund-mapping':return 'POST',{'form':{'fundCode':doc.fundCode},'gzip_response_allowed':True}
        if doc.kind=='fund-pcf':return 'GET',{'gzip_response_allowed':True}
    return 'GET',{}
