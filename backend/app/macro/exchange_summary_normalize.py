"""交易所具名聚合的纯规范化；空、null、零不合并。"""
from decimal import Decimal,InvalidOperation
import re

from app.macro.exchange_summary_plan import validate_exchange_plan

NAMES={
    'cn.market.margin_balance.sse':'上交所融资余额（交易所证券口径）',
    'cn.market.margin_balance.szse':'深交所融资余额（交易所证券口径）',
    'cn.market.margin_balance.bse':'北交所融资余额（单列，不入沪深比值）',
    'cn.market.a_share_float_mv.sh':'沪市A股流通市值',
    'cn.market.a_share_float_mv.sz':'深市A股流通市值',
}


def amount(value):
    if (type(value) not in (int,str) or len(str(value))>80
        or re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?',str(value)) is None):
        raise ValueError('EXCHANGE_AMOUNT_INVALID')
    try:
        number=Decimal(value)
        if not number.is_finite() or number<0 or number>=Decimal('1e18') or number.as_tuple().exponent < -100:
            raise ValueError()
        text=format(number,'f')
        if '.' in text:text=text.rstrip('0').rstrip('.')
        if '.' in text and len(text.split('.')[1])>10:raise ValueError()
        return '0' if number==0 else text.lstrip('+')
    except (InvalidOperation,ValueError,OverflowError):
        raise ValueError('EXCHANGE_AMOUNT_INVALID') from None


def normalize_exchange_response(plan,document,business_data):
    validate_exchange_plan(plan)
    if document not in plan['documents'] or type(business_data) is not dict or set(business_data)!={'fields','items'}:
        raise ValueError('EXCHANGE_RESPONSE_INVALID')
    fields,items=business_data['fields'],business_data['items']
    if (type(fields) is not list or any(type(f) is not str for f in fields)
        or len(fields)!=len(document['query']['fields']) or set(fields)!=set(document['query']['fields'])
        or type(items) is not list or len(items)>plan['limits']['maxResponseRows']):
        raise ValueError('EXCHANGE_RESPONSE_INVALID')
    result={'source':'tushare','status':'empty','points':[]}
    if not items:return result
    if type(items[0]) is not list or len(items[0])!=len(fields):
        raise ValueError('EXCHANGE_RESPONSE_INVALID')
    row=dict(zip(fields,items[0],strict=True))
    if any(type(row[k]) is not str or row[k]!=v for k,v in document['query']['params'].items()):
        raise ValueError('EXCHANGE_IDENTITY_INVALID')
    value=row['rzye' if plan['groupId']=='marginBalances' else 'float_mv']
    if value is None:return {**result,'status':'null'}
    point={'seriesId':document['seriesId'],'name':NAMES[document['seriesId']],
           'period':plan['targetDate'],'frequency':'daily','unit':plan['groupPolicy']['unit'],'value':amount(value)}
    return {**result,'status':'observed','points':[point]}
