"""统计局解读前两页的有界发现；不执行脚本、不猜报告链接。"""
from hashlib import sha256
from html.parser import HTMLParser
import re
from urllib.parse import urljoin

from app.macro.official_macro_normalize import _DirectoryLinks, _decode, _document, _month

ROOT='https://www.stats.gov.cn/xxgk/jd/sjjd2020/'
PAGE=ROOT+'index_1.html'
REPORT=r'https://www\.stats\.gov\.cn/xxgk/jd/sjjd2020/[0-9]{6}/t[0-9]{8}_[0-9]{6,12}\.html'
ROLES=('tier1-directory','tier1-directory-page')


class _Listing(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack=[];self.found=False;self.rows=0;self.links=_DirectoryLinks();self.script=None;self.scripts=[]

    def handle_starttag(self,tag,attrs):
        if self.script is not None:return
        if tag in ('script','style'):
            if self.stack:raise ValueError()
            self.script=tag;self.scripts.append('' if tag=='script' else None);return
        values=dict(attrs)
        if not self.stack:
            if tag=='ul' and values.get('class')=='list':
                if self.found:raise ValueError()
                self.found=True;self.stack=['ul']
            return
        if tag=='li':
            if self.stack!=['ul'] or values.get('class')!='list-item':raise ValueError()
            self.rows+=1
        elif len(self.stack)<2 or tag not in ('a','span','em','strong','b'):raise ValueError()
        self.stack.append(tag);self.links.handle_starttag(tag,attrs)

    def handle_endtag(self,tag):
        if self.script is not None:
            if tag==self.script:self.script=None
            return
        if not self.stack:return
        if self.stack[-1]!=tag:raise ValueError()
        self.links.handle_endtag(tag)
        if tag=='li' and len(self.links.links)!=self.rows:raise ValueError()
        self.stack.pop()

    def handle_data(self,data):
        if self.script=='script':self.scripts[-1]+=data
        elif self.stack:self.links.handle_data(data)


def validate_tier1_directory(value,target_month,page):
    if (type(page) is not int or page not in (0,1) or type(value) is not dict
        or set(value)!={'schemaVersion','role','source','rawSha256','targetMonth','page','candidateCount','selected'}
        or value['schemaVersion']!='1.0' or value['role']!=ROLES[page] or value['source']!='nbs'
        or type(value['rawSha256']) is not str or re.fullmatch('[0-9a-f]{64}',value['rawSha256']) is None
        or value['targetMonth']!=target_month or type(value['page']) is not int or value['page']!=page
        or type(value['candidateCount']) is not int or value['candidateCount'] not in (0,1)
        or (value['candidateCount']==0)!=(value['selected'] is None)):raise ValueError('OFFICIAL_TIER1_DIRECTORY_INVALID')
    _month(target_month)
    s=value['selected']
    if s is not None and (type(s) is not dict or set(s)!={'period','url'} or s['period']!=target_month
        or type(s['url']) is not str or re.fullmatch(REPORT,s['url']) is None):raise ValueError('OFFICIAL_TIER1_DIRECTORY_INVALID')


def parse_tier1_directory(body,target_month,*,page,max_candidates):
    try:
        target=_month(target_month)
        if type(page) is not int or page not in (0,1) or type(max_candidates) is not int or not 1<=max_candidates<=2:raise ValueError()
        _,text=_document(body)
        if '数据解读' not in text:raise ValueError()
        parser=_Listing();parser.feed(_decode(body));parser.close()
        if parser.stack or parser.script is not None or not parser.found or parser.rows!=10 or parser.links.href is not None:raise ValueError()
        scripts='\n'.join(s for s in parser.scripts if s is not None)
        for key,expected in [('m_nCurrPage',page),('m_nPageSize',10)]:
            if re.findall(r'\bvar\s+'+key+r'\s*=\s*([0-9]+)\s*;',scripts)!=[str(expected)]:raise ValueError()
        counts=re.findall(r'\bvar\s+m_nRecordCount\s*=\s*"([0-9]+)"\s*;',scripts)
        if len(counts)!=1 or not 20<=int(counts[0])<=100000:raise ValueError()
        urls=set()
        for href,label in parser.links.links:
            label=re.sub(r'\s+','',label)
            match=re.fullmatch(r'(?:国家统计局城市司[^年]{0,40}解读)?([0-9]{4})年([0-9]{1,2})月份商品住宅销售价格变动情况(?:统计数据|解读)?',label)
            if match is None or (int(match[1]),int(match[2]))!=(target.year,target.month):continue
            if href!=href.strip() or any(ord(c)<33 for c in href) or any(c in href for c in ('..','%','\\')):raise ValueError()
            url=urljoin(ROOT,href)
            if re.fullmatch(REPORT,url) is None:raise ValueError()
            urls.add(url)
        if len(urls)>1:raise ValueError()
        value=dict(schemaVersion='1.0',role=ROLES[page],source='nbs',rawSha256=sha256(body).hexdigest(),targetMonth=target_month,
            page=page,candidateCount=len(urls),selected=dict(period=target_month,url=next(iter(urls))) if urls else None)
        validate_tier1_directory(value,target_month,page)
        return value
    except Exception:raise ValueError('OFFICIAL_TIER1_DIRECTORY_INVALID') from None


def select_tier1_report(pages):
    if type(pages) is not list or len(pages)!=2:raise ValueError('OFFICIAL_TIER1_DIRECTORY_PREFIX_REQUIRED')
    for i,value in enumerate(pages):validate_tier1_directory(value,pages[0]['targetMonth'],i)
    candidates={p['selected']['url'] for p in pages if p['selected'] is not None}
    if len(candidates)>1:raise ValueError('OFFICIAL_TIER1_REPORT_AMBIGUOUS')
    return dict(period=pages[0]['targetMonth'],url=next(iter(candidates))) if candidates else None
