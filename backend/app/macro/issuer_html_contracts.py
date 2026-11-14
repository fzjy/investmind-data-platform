"""有界发行人HTML字段解析；元数据、PCF和成员见证是不同层次。"""
from dataclasses import dataclass,field
from datetime import date
from decimal import Decimal
from hashlib import sha256
from html.parser import HTMLParser
import re


@dataclass
class _Node:
    tag:str
    attrs:dict=field(default_factory=dict)
    children:list=field(default_factory=list)

    def nodes(self,tag=None):
        todo=[self];out=[]
        while todo:
            node=todo.pop()
            if tag is None or node.tag==tag:out.append(node)
            todo.extend(n for n in reversed(node.children) if isinstance(n,_Node))
        return out

    def text(self):
        todo=list(reversed(self.children));parts=[]
        while todo:
            item=todo.pop()
            if isinstance(item,str):parts.append(item)
            elif item.tag not in ('script','style'):todo.extend(reversed(item.children))
        return re.sub(r'\s+',' ',''.join(parts)).strip()


class _HTML(HTMLParser):
    VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True);self.root=_Node('root');self.stack=[self.root];self.count=0

    def handle_starttag(self,tag,attrs):
        self.count+=1
        if self.count>30000 or len(self.stack)>=128:raise ValueError()
        if len({k for k,_ in attrs})!=len(attrs):raise ValueError()
        node=_Node(tag,dict(attrs));self.stack[-1].children.append(node)
        if tag not in self.VOID:self.stack.append(node)

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.VOID:self.handle_endtag(tag)

    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:self.stack=self.stack[:i];break

    def handle_data(self,data):
        self.stack[-1].children.append(data)


def _one(values):
    if len(values)!=1:raise ValueError()
    return values[0]


def _id(root,value):return _one([n for n in root.nodes() if n.attrs.get('id')==value])
def _class(root,tag,value):return [n for n in root.nodes(tag) if value in (n.attrs.get('class') or '').split()]


def _document(body):
    if type(body) is not bytes or not 1<=len(body)<=8388608:raise ValueError()
    # 该发行人实际正文为GB编码而meta声明UTF-8；两者都严格解码，绝不替换损坏字符。
    try:text=body.decode('utf-8-sig');encoding='utf-8'
    except UnicodeError:text=body.decode('gb18030');encoding='gb18030'
    parser=_HTML();parser.feed(text);parser.close()
    return parser.root,encoding


def _consumer_identity(root):
    if _id(root,'fundid').attrs.get('value')!='159928':raise ValueError()
    heading=_one(_class(root,'h1','H1Pro'))
    code=_one(_class(heading,'span','fundNum')).text()
    if re.findall(r'(?<![0-9])[0-9]{6}(?![0-9])',code)!=['159928']:raise ValueError()
    name=re.sub(r'\s+','',''.join(s for s in heading.children if isinstance(s,str)))
    if name!='汇添富中证主要消费ETF':raise ValueError()
    return name


def parse_issuer_html_metadata(body,fund_code):
    """仅确认页面产品及字段语义，不根据基金名/比较基准推导跟踪关系。"""
    try:
        root,encoding=_document(body)
        value={'schemaVersion':'1.0','fundCode':fund_code,'encoding':encoding,'trackingIndexName':None,
            'trackingIndexCode':None,'benchmarkIndexName':None,'benchmarkIndexCode':None,
            'rawSha256':sha256(body).hexdigest(),'membershipProven':False}
        if fund_code=='512800':
            if _id(root,'fundCode').attrs.get('value')!=fund_code:raise ValueError()
            scope=_id(root,'fundDetailsList1')
            if _id(scope,'run_way').text()!=fund_code:raise ValueError()
            name=_id(scope,'tem_name').text()
            if name!='华宝中证银行交易型开放式指数证券投资基金':raise ValueError()
            declaration=_id(scope,'invest_scope').text()
            matches=re.findall(r'本基金的标的指数为([^。]+)。',declaration)
            if matches!=['中证银行指数'] or _id(scope,'floating_income_rate').text()!=matches[0]:raise ValueError()
            value.update(fundName=name,trackingIndexName=matches[0],benchmarkIndexName=matches[0])
        elif fund_code=='159928':
            name=_consumer_identity(root);scope=_id(root,'rigInfo1');fields={}
            for p in scope.nodes('p'):
                labels=p.nodes('strong');spans=p.nodes('span')
                if len(labels)==1 and len(spans)==1:
                    label=labels[0].text()
                    if label in ('基准指数代码：','基准指数简称：'):
                        if label in fields:raise ValueError()
                        fields[label]=spans[0].text()
            if fields!={'基准指数代码：':'000932.SH','基准指数简称：':'中证主要消费指数'}:raise ValueError()
            value.update(fundName=name,benchmarkIndexCode=fields['基准指数代码：'],benchmarkIndexName=fields['基准指数简称：'])
        else:raise ValueError()
        return value
    except Exception:raise ValueError('ISSUER_HTML_METADATA_INVALID') from None


def parse_consumer_pcf(body,fund_code,trading_date,*,max_members=10000):
    """消费ETF单页PCF；核对声明总数，不替代CSI同日一致性和数据库动态交集。"""
    try:
        if (fund_code!='159928' or type(trading_date) is not str or date.fromisoformat(trading_date).isoformat()!=trading_date
            or type(max_members) is not int or not 1<=max_members<=10000):raise ValueError()
        root,_=_document(body);_consumer_identity(root)
        if _id(root,'totalPages').attrs.get('value')!='1':raise ValueError()
        scope=_id(root,'etfdata')
        if _id(scope,'enddate').attrs.get('value')!=trading_date:raise ValueError()
        info=_class(scope,'table','infotable');fields={}
        if not 1<=len(info)<=3:raise ValueError()
        for row in [row for table in info for row in table.nodes('tr')]:
            labels=[c for c in row.children if isinstance(c,_Node) and c.tag=='th']
            cells=[c for c in row.children if isinstance(c,_Node) and c.tag=='td']
            if len(labels)!=1 or len(cells)!=1:raise ValueError()
            key=labels[0].text()
            if key in fields:raise ValueError()
            fields[key]=cells[0].text()
        if (fields.get('最新公告日期')!=trading_date or fields.get('基金名称')!='消费ETF汇添富'
            or fields.get('基金管理公司名称')!='汇添富基金管理股份有限公司' or fields.get('证券代码')!=fund_code):raise ValueError()
        count=re.fullmatch(r'([1-9][0-9]{0,4})\s*只',fields.get('全部申购赎回组合证券只数',''))
        if count is None or int(count[1])>max_members:raise ValueError()
        table=_one(_class(_id(root,'mfnd2'),'table','sharetable'));rows=table.nodes('tr')
        columns=['证券代码','证券简称','股票数量','现金替代标志','现金替代溢价比例','折价比例','申购代替金额','赎回代替金额','挂牌市场']
        if not rows or [n.text() for n in rows[0].nodes('th')]!=columns or len(rows)-1!=int(count[1]):raise ValueError()
        members=set();codes=set();cash=[];exchanges={'上海证券交易所':'SH','深圳证券交易所':'SZ','北京证券交易所':'BJ'}
        for row in rows[1:]:
            cells=[n.text() for n in row.nodes('td')]
            if len(cells)!=len(columns):raise ValueError()
            code,name,quantity,substitution,*_=cells
            if re.fullmatch('[0-9]{6}',code) is None or code in codes or not name or substitution not in ('允许','必须','禁止'):raise ValueError()
            if re.fullmatch(r'(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)(?:\.[0-9]+)?',quantity) is None:raise ValueError()
            if not 0<=Decimal(quantity.replace(',',''))<Decimal('1e18') or cells[-1] not in exchanges:raise ValueError()
            codes.add(code);qualified=code+'.'+exchanges[cells[-1]]
            if code=='159900' or name=='申赎现金':
                if (code!='159900' or name!='申赎现金' or Decimal(quantity.replace(',',''))!=0
                    or substitution!='必须' or cells[-1]!='深圳证券交易所'):raise ValueError()
                cash.append(qualified)
            else:members.add(qualified)
        if not members:raise ValueError()
        return {'schemaVersion':'1.0','fundCode':fund_code,'asOfDate':trading_date,'memberCodes':sorted(members),
            'memberCount':len(members),'componentRecordCount':int(count[1]),'excludedCashComponents':cash,
            'membershipBasis':'published-pcf-all-components-not-fund-holdings','rawSha256':sha256(body).hexdigest()}
    except Exception:raise ValueError('ISSUER_CONSUMER_PCF_INVALID') from None
