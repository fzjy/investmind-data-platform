"""财政部统一月报目录，精确月期单页发现；不猜URL、不补空月。"""
from hashlib import sha256
import re
from urllib.parse import urljoin
from app.macro.official_macro_normalize import _DirectoryLinks,_decode,_document,_month

DIRECTORY_URL='https://zwgls.mof.gov.cn/tjsj/'
REPORT_URL=r'https://zwgls\.mof\.gov\.cn/tjsj/[0-9]{6}/t[0-9]{8}_[0-9]{6,12}\.htm'


def parse_special_directory(body:bytes,target_month:str,*,max_candidates:int)->dict:
    try:
        target=_month(target_month)
        if type(max_candidates) is not int or not 1<=max_candidates<=2:raise ValueError()
        titles,_=_document(body)
        if not any(re.sub(r'\s+','',title)=='统计数据' for title in titles):raise ValueError()
        parser=_DirectoryLinks();parser.feed(_decode(body));parser.close()
        if parser.href is not None or parser.ignored is not None:raise ValueError()
        urls=set()
        for href,label in parser.links:
            match=re.fullmatch(r'([0-9]{4})年([0-9]{1,2})月地方政府债券发行和债务余额情况',re.sub(r'\s+','',label))
            if match is None:continue
            if _month(f'{int(match[1]):04}-{int(match[2]):02}')!=target:continue
            if href!=href.strip() or any(ord(c)<33 for c in href) or any(c in href for c in ('..','%','\\')):raise ValueError()
            url=urljoin(DIRECTORY_URL,href)
            if re.fullmatch(REPORT_URL,url) is None:raise ValueError()
            urls.add(url)
        if len(urls)>1 or len(urls)>max_candidates:raise ValueError()
        return dict(schemaVersion='1.0',role='special-directory',source='mof',rawSha256=sha256(body).hexdigest(),
            targetMonth=target_month,candidateCount=len(urls),selected=dict(period=target_month,url=next(iter(urls))) if urls else None)
    except Exception:raise ValueError('OFFICIAL_SPECIAL_DIRECTORY_INVALID') from None
