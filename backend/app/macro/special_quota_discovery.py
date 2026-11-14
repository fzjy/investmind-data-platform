"""年度额度仅沿财政部真实链接发现，不构造年度页或报告路径。"""
from hashlib import sha256
import re
from urllib.parse import urljoin
from app.macro.official_macro_normalize import _DirectoryLinks,_decode,_document

BUDGET_ROOT='https://www.mof.gov.cn/zyyjsgkpt/zyzfyjs/zyys/ysbb/'
ROLES=('special-budget-directory','special-year-directory')

def quota_link(role,year,url):
    pattern=(r'https://yss\.mof\.gov\.cn/'+str(year)+r'zyczys/' if role==ROLES[0] else
        r'https://yss\.mof\.gov\.cn/'+str(year)+r'zyczys/[0-9]{6}/t[0-9]{8}_[0-9]{6,12}\.htm')
    return type(url) is str and re.fullmatch(pattern,url) is not None

def parse_quota_directory(body,role,year,*,max_candidates):
    try:
        if role not in ROLES or type(year) is not int or not 1001<=year<=9999 or type(max_candidates) is not int or not 1<=max_candidates<=2:raise ValueError()
        titles,_=_document(body);expected='预算报表' if role==ROLES[0] else f'{year}年中央财政预算'
        if expected not in [re.sub(r'\s+','',t) for t in titles]:raise ValueError()
        links=_DirectoryLinks();links.feed(_decode(body));links.close()
        if links.href is not None or links.ignored is not None:raise ValueError()
        label=f'{year}年中央财政预算' if role==ROLES[0] else f'{year-1}年和{year}年地方政府专项债务余额情况表'
        urls=set();base=BUDGET_ROOT if role==ROLES[0] else f'https://yss.mof.gov.cn/{year}zyczys/'
        for href,text in links.links:
            if re.sub(r'\s+','',text)!=label:continue
            if href!=href.strip() or any(ord(c)<33 for c in href) or any(c in href for c in ('..','%','\\')):raise ValueError()
            url=urljoin(base,href)
            # 官方总目录实际使用此HTTP链接；仅此精确同年目录升级HTTPS，绝不发明路径。
            if role==ROLES[0] and url==f'http://yss.mof.gov.cn/{year}zyczys/':url='https:'+url[5:]
            if not quota_link(role,year,url):raise ValueError()
            urls.add(url)
        if len(urls)>1:raise ValueError()
        return dict(schemaVersion='1.0',role=role,source='mof',rawSha256=sha256(body).hexdigest(),targetYear=str(year),
            candidateCount=len(urls),selected=dict(year=str(year),url=next(iter(urls))) if urls else None)
    except Exception:raise ValueError('OFFICIAL_SPECIAL_QUOTA_DIRECTORY_INVALID') from None
