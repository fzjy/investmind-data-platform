"""央行住户贷款目标月目录发现；纯解析，不猜URL、不翻页或请求网络。"""
from hashlib import sha256
import re
from urllib.parse import urljoin

from app.macro.official_macro_normalize import _DirectoryLinks, _decode, _document, _month, _report_months

DIRECTORY_URL = 'https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html'
REPORT_URL = r'https://www\.pbc\.gov\.cn/diaochatongjisi/116219/116225/[0-9]{17,22}/index\.html'


def parse_household_directory(body: bytes, target_month: str, *, max_candidates: int) -> dict:
    try:
        target = _month(target_month)
        if type(max_candidates) is not int or not 1 <= max_candidates <= 2:
            raise ValueError()
        titles, _ = _document(body)
        if not any(re.fullmatch(r'数据解读(?:[-_—－](?:中国人民银行|调查统计司))?', re.sub(r'\s+', '', title)) for title in titles):
            raise ValueError()
        parser = _DirectoryLinks()
        parser.feed(_decode(body))
        parser.close()
        if parser.href is not None or parser.ignored is not None:
            raise ValueError()
        urls = set()
        for href, label in parser.links:
            label = re.sub(r'\s+', '', label)
            if re.fullmatch(r'[0-9]{4}年.*金融统计(?:数据)?报告', label) is None:
                continue
            if _report_months((label,)) != {target}:
                continue
            if href != href.strip() or any(ord(c) < 33 for c in href) or any(c in href for c in ('..', '%', '\\')):
                raise ValueError()
            url = urljoin(DIRECTORY_URL, href)
            if re.fullmatch(REPORT_URL, url) is None:
                raise ValueError()
            urls.add(url)
        # 同月不同链接可能是修订或不同版本，不能按顺序/路径猜最新。
        if len(urls) > 1 or len(urls) > max_candidates:
            raise ValueError()
        return dict(schemaVersion='1.0', role='household-directory', source='pboc',
            rawSha256=sha256(body).hexdigest(), targetMonth=target_month, candidateCount=len(urls),
            selected=dict(period=target_month, url=next(iter(urls))) if urls else None)
    except Exception:
        raise ValueError('OFFICIAL_HOUSEHOLD_DIRECTORY_INVALID') from None
