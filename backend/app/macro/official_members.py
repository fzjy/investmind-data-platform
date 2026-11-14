"""官方XLS原件的纯解析；不发送网络，不按固定成分数量判定完整性。"""
from datetime import datetime
from datetime import date
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from html.parser import HTMLParser
import math
import re
from xml.etree import ElementTree

import xlrd

HEADERS=(('日期',),('指数代码',),('指数名称',),('指数英文名称',),
    ('成份券代码','成分券代码'),('成份券名称','成分券名称'),
    ('成份券英文名称','成分券英文名称'),('交易所',),('交易所英文名称',))
ENGLISH_HEADERS=('Date','Index Code','Index Name','Index Name(Eng)','Constituent Code',
    'Constituent Name','Constituent Name(Eng)','Exchange','Exchange(Eng)')
EXCHANGES={
    '上海证券交易所':('SH',{'Shanghai Stock Exchange','Shanghai','SSE'}),
    '深圳证券交易所':('SZ',{'Shenzhen Stock Exchange','Shenzhen','SZSE'}),
    '北京证券交易所':('BJ',{'Beijing Stock Exchange','Beijing','BSE'}),
}


def _text(value):
    if type(value) is not str or not value.strip() or len(value)>2000:
        raise ValueError()
    return value.strip()


def _digits(cell,length):
    if cell.ctype==xlrd.XL_CELL_NUMBER:
        value=cell.value
        if not math.isfinite(value) or value!=int(value) or not 0<=value<10**length:raise ValueError()
        return f'{int(value):0{length}d}'
    if cell.ctype!=xlrd.XL_CELL_TEXT or re.fullmatch('[0-9]{'+str(length)+'}',cell.value) is None:
        raise ValueError()
    return cell.value


def parse_csi_members(body,index_code,*,max_members=10000):
    """完整BIFF工作簿、表身份/日期/成员一致；可信使用还须验证来源账本。"""
    book=None
    try:
        if (type(body) is not bytes or not 8<=len(body)<=8388608
            or not (body.startswith(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1') or body.startswith(b'\x09\x08'))
            or type(index_code) is not str or re.fullmatch('[0-9]{6}',index_code) is None
            or type(max_members) is not int or not 1<=max_members<=10000):raise ValueError()
        book=xlrd.open_workbook(file_contents=body,logfile=StringIO(),formatting_info=True,on_demand=True)
        if book.biff_version!=80 or book.nsheets!=1:raise ValueError()
        sheet=book.sheet_by_index(0)
        if not 2<=sheet.nrows<=max_members+1 or sheet.ncols!=9 or sheet.merged_cells:
            raise ValueError()
        for column,labels in enumerate(HEADERS):
            compact=lambda text:re.sub(r'\s+','',text).casefold()
            text=compact(_text(sheet.cell_value(0,column)))
            accepted={compact(label+suffix) for label in labels for suffix in ('',ENGLISH_HEADERS[column])}
            if text not in accepted:raise ValueError()
        members=set();identity=None
        for r in range(1,sheet.nrows):
            info=sheet.rowinfo_map.get(r)
            if info is not None and info.hidden:raise ValueError()
            cells=sheet.row(r)
            if any(c.ctype in (xlrd.XL_CELL_ERROR,xlrd.XL_CELL_EMPTY,xlrd.XL_CELL_BLANK,xlrd.XL_CELL_BOOLEAN) for c in cells):raise ValueError()
            day=_digits(cells[0],8)
            if datetime.strptime(day,'%Y%m%d').strftime('%Y%m%d')!=day:raise ValueError()
            if _digits(cells[1],6)!=index_code:raise ValueError()
            current=(day,_text(cells[2].value),_text(cells[3].value))
            if identity is not None and current!=identity:raise ValueError()
            identity=current
            code=_digits(cells[4],6);_text(cells[5].value);_text(cells[6].value)
            exchange=EXCHANGES.get(_text(cells[7].value))
            if exchange is None or _text(cells[8].value) not in exchange[1]:raise ValueError()
            symbol=code+'.'+exchange[0]
            if symbol in members:raise ValueError()
            members.add(symbol)
        return {'schemaVersion':'1.0','indexCode':index_code,'indexName':identity[1],
            'indexEnglishName':identity[2],'asOfDate':datetime.strptime(identity[0],'%Y%m%d').date().isoformat(),
            'members':sorted(members),'memberCount':len(members),'rawSha256':sha256(body).hexdigest()}
    except Exception:
        raise ValueError('OFFICIAL_MEMBERS_INVALID') from None
    finally:
        if book is not None:book.release_resources()


class _FundText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored=0;self.heading=None;self.headers=[];self.text=[]

    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.ignored+=1
        if self.ignored:return
        if tag=='h3':self.heading=[]
        if tag in ('p','div','br','h3'):self.text.append('\n')

    def handle_endtag(self,tag):
        if tag in ('script','style'):
            self.ignored=max(0,self.ignored-1);return
        if self.ignored:return
        if tag=='h3' and self.heading is not None:
            self.headers.append(''.join(self.heading));self.heading=None
        if tag in ('p','div','h3'):self.text.append('\n')

    def handle_data(self,data):
        if self.ignored:return
        self.text.append(data)
        if self.heading is not None:self.heading.append(data)


def parse_fund_mapping(body,fund_code):
    try:
        if (type(body) is not bytes or not 1<=len(body)<=8388608
            or type(fund_code) is not str or re.fullmatch('[0-9]{6}',fund_code) is None):raise ValueError()
        parser=_FundText();parser.feed(body.decode('utf-8-sig'));parser.close()
        headers=[re.sub(r'\s+','',h) for h in parser.headers]
        products=[re.fullmatch(r'(.+)[（(]([0-9]{6})[）)]',h) for h in headers]
        products=[p for p in products if p]
        if len(products)!=1 or products[0][2]!=fund_code:raise ValueError()
        text=re.sub(r'[\t\r \u3000]+','',''.join(parser.text))
        names=set(re.findall(r'本基金的标的指数为([^。\n]+?)及其未来可能发生的变更。',text))
        if len(names)!=1:raise ValueError()
        name=names.pop()
        if not name.endswith('指数') or not 3<=len(name)<=100:raise ValueError()
        return {'schemaVersion':'1.0','fundCode':fund_code,'fundName':products[0][1],
            'trackingIndexName':name,'rawSha256':sha256(body).hexdigest()}
    except Exception:raise ValueError('OFFICIAL_FUND_MAPPING_INVALID') from None


def parse_fund_pcf(body,fund_code,trading_date,*,max_members=10000):
    try:
        if (type(body) is not bytes or not 1<=len(body)<=8388608 or type(fund_code) is not str
            or re.fullmatch('[0-9]{6}',fund_code) is None or type(trading_date) is not str
            or date.fromisoformat(trading_date).isoformat()!=trading_date
            or type(max_members) is not int or not 1<=max_members<=10000):raise ValueError()
        text=body.decode('utf-8-sig')
        if re.search(r'<!\s*(?:DOCTYPE|ENTITY)',text,re.I):raise ValueError()
        root=ElementTree.fromstring(text)
        if root.tag!='SSEPortfolioCompositionFile' or root.attrib:raise ValueError()
        tags=[c.tag for c in root]
        if len(tags)!=len(set(tags)) or len(tags)>100:raise ValueError()
        def scalar(parent,name):
            matches=parent.findall(name)
            if len(matches)!=1 or matches[0].attrib or len(matches[0]):raise ValueError()
            return _text(matches[0].text)
        if scalar(root,'FundInstrumentID')!=fund_code or scalar(root,'TradingDay')!=trading_date.replace('-',''):raise ValueError()
        count=scalar(root,'RecordNumber')
        if re.fullmatch('[1-9][0-9]{0,4}',count) is None or int(count)>max_members:raise ValueError()
        container=root.find('ComponentList')
        if container is None or container.attrib or len(container)!=int(count):raise ValueError()
        members=set()
        for component in container:
            if component.tag!='Component' or component.attrib or len(component)>40:raise ValueError()
            names=[c.tag for c in component]
            if len(names)!=len(set(names)):raise ValueError()
            code=scalar(component,'InstrumentID')
            if re.fullmatch('[0-9]{6}',code) is None or code in members:raise ValueError()
            quantity=Decimal(scalar(component,'Quantity'))
            if not quantity.is_finite() or not 0<=quantity<Decimal('1e18'):raise ValueError()
            if re.fullmatch('[0-9]',scalar(component,'SubstitutionFlag')) is None:raise ValueError()
            members.add(code)
        return {'schemaVersion':'1.0','fundCode':fund_code,'asOfDate':trading_date,'memberCodes':sorted(members),
            'memberCount':len(members),'membershipBasis':'published-pcf-all-components-not-fund-holdings',
            'rawSha256':sha256(body).hexdigest()}
    except Exception:raise ValueError('OFFICIAL_PCF_INVALID') from None
