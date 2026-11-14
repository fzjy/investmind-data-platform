"""官方宏观文字原件纯规范化；不请求网络，不用余额/同比代替累计净增。"""
from datetime import date
from decimal import Decimal,localcontext
from hashlib import sha256
from html.parser import HTMLParser
import re
from calendar import monthrange
from urllib.parse import urljoin

from app.macro.fiscal_official import _decode, _TableParser

_CHINESE=('一','二','三','四','五','六','七','八','九','十','十一','十二')
_UNSIGNED=r'(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?'
_ORDINAL=r'(?:[一二三四五六七八九十]+|[0-9]+)[、.．]'


class _DocumentText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored=0;self.heading=None;self.heading_tag=None;self.titles=[];self.parts=[]

    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.ignored+=1
        if self.ignored:return
        values={k.lower():v for k,v in attrs}
        if tag=='meta' and (values.get('name') or '').lower() in ('articletitle','contenttitle'):
            if values.get('content'):self.titles.append(values['content'])
        if tag in ('h1','h2','title'):
            if self.heading is not None:raise ValueError('ambiguous heading')
            self.heading=[];self.heading_tag=tag
        if tag in ('p','div','br','h1','h2','h3','li','tr'):self.parts.append('\n')

    def handle_endtag(self,tag):
        if tag in ('script','style'):
            self.ignored=max(0,self.ignored-1);return
        if self.ignored:return
        if tag==self.heading_tag:
            self.titles.append(''.join(self.heading));self.heading=None;self.heading_tag=None
        if tag in ('p','div','h1','h2','h3','li','tr'):self.parts.append('\n')

    def handle_data(self,data):
        if self.ignored:return
        self.parts.append(data)
        if self.heading is not None:self.heading.append(data)


def _document(body):
    if type(body) is not bytes or not 1<=len(body)<=5242880:raise ValueError()
    parser=_DocumentText();parser.feed(_decode(body));parser.close()
    if parser.heading is not None or not parser.titles:raise ValueError()
    return parser.titles,re.sub(r'[^\S\n]+',' ',''.join(parser.parts))


def _month(period):
    if type(period) is not str or re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])',period) is None:raise ValueError()
    value=date.fromisoformat(period+'-01')
    return value


def _decimal(text):
    if type(text) is not str or len(text)>40 or re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',text) is None:raise ValueError()
    value=Decimal(text)
    if abs(value)>=Decimal('1e18'):raise ValueError()
    return value


def _decimal_text(value):
    if not value:return '0'
    text=format(value,'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _scope_aliases(month):
    aliases={f'前{month}个月',f'前{_CHINESE[month-1]}个月'}
    if month==1:aliases.update(('1月','1月份','一月','一月份'))
    if month==3:aliases.update(('一季度','第一季度'))
    if month==6:aliases.add('上半年')
    if month==9:aliases.add('前三季度')
    if month==12:aliases.update(('','全年'))
    return aliases


def _report_months(titles):
    periods=set()
    for title in titles:
        for match in re.finditer(r'([0-9]{4})\s*年\s*([^\n]*?)\s*金融统计(?:数据)?报告',title):
            year=int(match[1]);scope=re.sub(r'\s+','',match[2])
            months=[m for m in range(1,13) if scope in _scope_aliases(m) or scope in (f'{m}月',f'{m}月份')]
            if len(months)!=1:raise ValueError()
            periods.add(date(year,months[0],1))
    return periods


def _evidence(body,role,period,source,points,events=None):
    return {'schemaVersion':'1.0','role':role,'period':period,'source':source,
        'rawSha256':sha256(body).hexdigest(),'points':points,'events':events or []}


def parse_household_ytd(body,period):
    try:
        month=_month(period);titles,text=_document(body)
        if _report_months(titles)!={month}:raise ValueError()
        scopes='|'.join(re.escape(s) for s in sorted(_scope_aliases(month.month),key=len,reverse=True))
        loan=re.compile(r'(?m)^\s*'+_ORDINAL+r'\s*(?:'+str(month.year)+r'\s*年\s*)?(?:'+scopes+r')\s*人民币(?:各项)?贷款')
        headings=list(loan.finditer(text))
        if len(headings)!=1:raise ValueError()
        start=headings[0].end()
        next_heading=re.search(r'(?m)^\s*'+_ORDINAL,text[start:])
        end=start+next_heading.start() if next_heading else len(text)
        section=text[start:end]
        matches=list(re.finditer(r'(?<!外币)住户贷款[^;；。]{0,300}?中长期贷款\s*(增加|减少)\s*('+_UNSIGNED+r')\s*(万亿元|亿元)',section))
        if len(matches)!=1:raise ValueError()
        match=matches[0];amount=_decimal(match[2].replace(',',''))
        with localcontext() as context:
            context.prec=80
            amount*=10000 if match[3]=='万亿元' else 1
            amount*= -1 if match[1]=='减少' else 1
        if abs(amount)>=Decimal('1e18'):raise ValueError()
        point={'seriesId':'cn.credit.household_medium_long_term_ytd','period':month.isoformat(),
            'name':'人民币住户中长期贷款年初累计净增','value':_decimal_text(amount),'unit':'亿元','frequency':'monthly'}
        return _evidence(body,'household-ytd',month.isoformat(),'pboc',[point])
    except Exception:raise ValueError('OFFICIAL_HOUSEHOLD_DOCUMENT_INVALID') from None


def derive_household_months(values):
    """输入须是同一已核系列的可见累计值；一月直接取值，不跨年相减或平摊缺月。"""
    try:
        if type(values) is not dict:raise ValueError()
        parsed={}
        for period,value in values.items():
            if type(period) is not str or len(period)!=10 or _month(period[:7]).isoformat()!=period:raise ValueError()
            parsed[period]=_decimal(value)
        result=[]
        for period,value in sorted(parsed.items()):
            month=date.fromisoformat(period)
            if month.month==1:delta=value
            else:
                previous=f'{month.year:04}-{month.month-1:02}-01'
                with localcontext() as context:
                    context.prec=80
                    delta=value-parsed[previous] if previous in parsed else None
            result.append({'month':period,'value':_decimal_text(delta) if delta is not None else None})
        return result
    except Exception:raise ValueError('OFFICIAL_HOUSEHOLD_CUMULATIVE_INVALID') from None


def _housing_rate(text,housing_type):
    name='新建商品住宅' if housing_type=='new' else '二手住宅'
    previous=r'(?:由上月(?:(?:上涨|下降)\s*'+_UNSIGNED+r'\s*[%％]|持平)\s*转为)?'
    current=r'(?:(上涨|下降)\s*('+_UNSIGNED+r')\s*[%％](?=\s*(?:[，,。；;（(]|$))|(?:连续(?:[一二两三四五六七八九十]|十一|十二|[1-9]|1[0-2])个?月)?持平(?=\s*(?:[，,。；;]|$)))'
    matches=list(re.finditer(r'(?<!非)一线城市'+name+r'销售价格环比\s*'+previous+current,text))
    if housing_type=='existing':
        # 历史解读常在同一句分号后省略主语；只继承已明确的一线新房环比，
        # 到句末/换行/任意城市层级即结束，不能从二线或同比借数。
        anchors=re.finditer(r'(?<!非)一线城市新建商品住宅销售价格环比\s*'+previous+current,text)
        for anchor in anchors:
            tail=re.split(r'[。！？\n]|[一二三]线城市',text[anchor.end():],maxsplit=1)[0]
            matches.extend(re.finditer(r'[；;]\s*二手住宅(?:销售价格)?环比\s*'+previous+current,tail))
    if len(matches)!=1:raise ValueError()
    match=matches[0]
    if match[1] is None:return '0'
    value=_decimal(match[2].replace(',',''))
    if match[1]=='下降':value=-value
    if value< -100:raise ValueError()
    return _decimal_text(value)


def parse_tier1_housing(body,period):
    try:
        month=_month(period);titles,text=_document(body);periods=set()
        for title in titles:
            if '住宅' not in title or '价格' not in title:continue
            for match in re.finditer(r'([0-9]{4})\s*年\s*([0-9]{1,2})\s*月(?:份)?',title):
                periods.add(date(int(match[1]),int(match[2]),1))
        if periods!={month}:raise ValueError()
        points=[]
        for kind,suffix,label in [('new','','新建商品住宅'),('existing','.existing','二手住宅')]:
            points.append({'seriesId':'cn.house.price_70_city_mom.tier1'+suffix,'period':month.isoformat(),
                'name':'一线城市'+label+'销售价格环比','value':_housing_rate(text,kind),'unit':'%',
                'frequency':'monthly','housingType':kind})
        return _evidence(body,'tier1-prices',month.isoformat(),'nbs',points)
    except Exception:raise ValueError('OFFICIAL_TIER1_HOUSING_INVALID') from None


def _positive_amount(text: str) -> Decimal:
    value = _decimal(text.replace(',', ''))
    if value < 0:
        raise ValueError()
    return value


def _official_point(series: str, period: str, name: str, value: Decimal, *, unit: str = '亿元', frequency: str = 'monthly') -> dict:
    return {'seriesId': series, 'period': period, 'name': name,
            'value': _decimal_text(value), 'unit': unit, 'frequency': frequency}


def parse_special_quota(body: bytes, year: int) -> dict:
    """按目标年新增专项额度行取值；表标题可为上一年余额表。"""
    try:
        if type(year) is not int or not 2000 <= year <= 2099:
            raise ValueError()
        _document(body)
        parser = _TableParser()
        parser.feed(_decode(body))
        parser.finish()
        found = []
        label = re.compile(r'(?:[一二三四五六七八九十0-9]+[、.．])?' + str(year) + r'年地方政府专项债务新增限额')
        for table in parser.tables:
            for row in table.rows:
                matches = [cell for cell in row if label.fullmatch(re.sub(r'\s+', '', cell.text))]
                if not matches:
                    continue
                # 单位必须在同表出现，不能从页面另一张表借用。
                units = re.findall(r'单位\s*[:：]\s*([^\s，,。;；]+)', '\n'.join(c.text for cells in table.rows for c in cells))
                if len(matches) != 1 or units != ['亿元']:
                    raise ValueError()
                numbers = [cell for cell in row if cell.column > matches[0].column and cell.text.strip()]
                if len(numbers) != 1 or numbers[0].rowspan != 1 or numbers[0].colspan != 1:
                    raise ValueError()
                raw = numbers[0].text.strip()
                if re.fullmatch(_UNSIGNED, raw) is None:
                    raise ValueError()
                found.append(_positive_amount(raw))
        if len(found) != 1 or found[0] <= 0:
            raise ValueError()
        period = f'{year}-01-01'
        point = _official_point('cn.fiscal.special_bonds_new_quota', period,
                                '年度地方政府新增专项债务原限额', found[0], frequency='annual')
        return _evidence(body, 'special-quota', period, 'mof', [point])
    except Exception:
        raise ValueError('OFFICIAL_SPECIAL_QUOTA_INVALID') from None


def _unique_amount(pattern: str, text: str) -> Decimal:
    values = re.findall(pattern, text)
    if len(values) != 1:
        raise ValueError()
    return _positive_amount(values[0])


def parse_special_issuance(body: bytes, period: str, role: str) -> dict:
    """新增专项直报月值/累计/进度分别保留，不由跨版本累计替代月值。"""
    try:
        month = _month(period)
        titles, text = _document(body)
        amount = '(' + _UNSIGNED + r')\s*亿元'
        if role == 'special-issuance':
            expected = f'{month.year}年{month.month}月地方政府债券发行和债务余额情况'
            if {re.sub(r'\s+', '', title) for title in titles if title.strip()} != {expected}:
                raise ValueError()
            # 两个有明确时间前缀的发行段，排除一般、再融资和余额段。
            def monthly_amount(scope: str) -> Decimal:
                pattern = (str(month.year) + r'\s*年\s*' + scope +
                           r'\s*[，,、]?\s*全国发行新增(?:地方政府)?债券[^。；;\n]{0,160}?' +
                           r'其中一般债券\s*' + _UNSIGNED + r'\s*亿元[、，,]\s*专项债券\s*' + amount)
                return _unique_amount(pattern, text)
            direct = monthly_amount(str(month.month) + r'\s*月')
            ytd = monthly_amount(r'1\s*[—－–-]\s*' + str(month.month) + r'\s*月')
            reported = None
            events = []
        elif role == 'special-briefing':
            quarter_names = {3: '一季度', 6: '上半年', 9: '前三季度', 12: '全年'}
            if month.month not in quarter_names:
                raise ValueError()
            expected = f'{month.year}年{quarter_names[month.month]}财政收支情况新闻发布会'
            actual_titles = {re.sub(r'\s+', '', title) for title in titles if title.strip()}
            if not actual_titles or not actual_titles <= {expected, expected+'文字实录'}:
                raise ValueError()
            special = r'(?:全国共)?发行新增(?:地方政府)?专项债券\s*'
            scope = r'1\s*[—－–-]\s*' + str(month.month) + r'\s*月\s*[，,]\s*' + special
            ytd = _unique_amount(scope + amount, text)
            contextual = re.findall(scope + _UNSIGNED + r'\s*亿元[，,]\s*发行进度\s*' +
                                    _UNSIGNED + r'\s*[%％][，,]\s*其中[，,]\s*' + str(month.month) +
                                    r'\s*月份发行\s*' + amount, text)
            explicit = re.findall(r'(?<![0-9—－–-])' + str(month.month) + r'\s*月\s*' + special + amount, text)
            if len(contextual)+len(explicit) != 1:
                raise ValueError()
            direct = _positive_amount((contextual+explicit)[0])
            progress = re.findall(r'完成全年新增专项债务限额的\s*(' + _UNSIGNED + r')\s*[%％]', text)
            progress += re.findall(scope + _UNSIGNED + r'\s*亿元[，,]\s*发行进度\s*(' + _UNSIGNED + r')\s*[%％]', text)
            if len(progress) != 1:
                raise ValueError()
            reported = _positive_amount(progress[0])
            events = []
            if '结存限额' in text:
                allocations = re.findall(r'结存限额[^。\n]{0,180}?其中一般债务\s*' + _UNSIGNED + r'\s*亿元[，,、]\s*专项债务\s*' + amount, text)
                allocations += re.findall(r'安排使用\s*(' + _UNSIGNED + r')\s*亿元专项债务结存限额', text)
                if len(allocations) != 1:
                    raise ValueError()
                carryover = _positive_amount(allocations[0])
                events.append({'kind': 'special-carryover-allocation', 'amount': _decimal_text(carryover),
                               'unit': '亿元', 'effectivePeriod': None})
        else:
            raise ValueError()
        stamp = month.isoformat()
        points = [
            _official_point('cn.fiscal.special_bonds_new_month', stamp, '地方政府新增专项债券当月发行额（直报）', direct),
            _official_point('cn.fiscal.special_bonds_new_ytd', stamp, '地方政府新增专项债券年初累计发行额', ytd),
        ]
        if reported is not None:
            points.append(_official_point('cn.fiscal.special_bonds_reported_progress', stamp,
                                          '新增专项债券发行进度（发布会直报）', reported, unit='%'))
        return _evidence(body, role, stamp, 'mof', points, events)
    except Exception:
        raise ValueError('OFFICIAL_SPECIAL_ISSUANCE_INVALID') from None


def derive_special_progress(quota: str, ytd: str, direct: str, prior: str | None, reported: str | None) -> dict:
    """只诊断跨发布版累计差异；月值始终取直报，结存额度不进入分母。"""
    quota_value = _decimal(quota)
    values = [_decimal(v) if v is not None else None for v in (ytd, direct, prior, reported)]
    if quota_value <= 0 or any(v is not None and v < 0 for v in values) or values[0] is None or values[1] is None:
        raise ValueError('OFFICIAL_SPECIAL_PROGRESS_INVALID')
    cumulative, month, previous, progress = values
    with localcontext() as context:
        context.prec = 40
        computed = cumulative / quota_value * 100
        difference = cumulative - previous if previous is not None else None
        discrepancy = month - difference if difference is not None else None
    return {'quota': quota, 'ytd': ytd, 'directMonth': direct, 'reportedProgress': reported,
            'computedProgress': _decimal_text(computed),
            'crossVintageDifference': _decimal_text(difference) if difference is not None else None,
            'differenceDiscrepancy': _decimal_text(discrepancy) if discrepancy is not None else None,
            'revisionConflict': discrepancy is not None and discrepancy != 0, 'derivedMonth': None}


def _quarter(quarter: str) -> tuple[int, int, date]:
    if type(quarter) is not str or re.fullmatch(r'20[0-9]{2}-Q[1-4]', quarter) is None:
        raise ValueError()
    year, number = int(quarter[:4]), int(quarter[-1])
    month = number * 3
    return year, number, date(year, month, monthrange(year, month)[1])


def _report_quarter(title: str) -> str | None:
    match = re.fullmatch(r'(20[0-9]{2})年(?:第)?([一二三四])季度金融机构贷款投向统计报告', re.sub(r'\s+', '', title))
    return f'{match[1]}-Q{"一二三四".index(match[2])+1}' if match else None


def parse_property_loan_report(body: bytes, quarter: str) -> dict:
    try:
        _, _, period = _quarter(quarter)
        titles, text = _document(body)
        report_periods = {_report_quarter(t) for t in titles if _report_quarter(t)}
        if report_periods != {quarter}:
            raise ValueError()
        matches = list(re.finditer(r'人民币房地产贷款余额\s*(' + _UNSIGNED +
                                  r')\s*(万亿元|亿元)\s*[，,]\s*同比\s*(增长|下降)\s*(' +
                                  _UNSIGNED + r')\s*[%％]', text))
        if len(matches) != 1:
            raise ValueError()
        match = matches[0]
        with localcontext() as context:
            context.prec = 80
            balance = _positive_amount(match[1]) * (10000 if match[2] == '万亿元' else 1)
        change = _positive_amount(match[4]) * (-1 if match[3] == '下降' else 1)
        if balance >= Decimal('1e18') or change < -100:
            raise ValueError()
        stamp = period.isoformat()
        points = [
            _official_point('cn.credit.property_loans_balance', stamp, '人民币房地产贷款余额', balance, frequency='quarterly'),
            _official_point('cn.credit.property_loans_yoy', stamp, '人民币房地产贷款余额同比', change, unit='%', frequency='quarterly'),
        ]
        return _evidence(body, 'property-report', stamp, 'pboc', points)
    except Exception:
        raise ValueError('OFFICIAL_PROPERTY_REPORT_INVALID') from None


class _DirectoryLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.label: list[str] = []
        self.ignored: str | None = None

    def handle_starttag(self, tag, attrs):
        if self.ignored is not None:
            return
        if tag in ('script', 'style'):
            self.ignored = tag
        elif tag == 'a':
            if self.href is not None:
                raise ValueError()
            urls = [v for k, v in attrs if k == 'href']
            if len(urls)>1:
                raise ValueError()
            # 保留无href/空href的锚点标题，目标报告不得在来源检查前被丢弃。
            self.href, self.label = (urls[0] or '') if urls else '', []

    def handle_data(self, data):
        if self.ignored is None and self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if self.ignored is not None:
            if tag == self.ignored:
                self.ignored = None
            return
        if tag == 'a' and self.href is not None:
            self.links.append((self.href, ''.join(self.label)))
            self.href = None
            if len(self.links) > 2048:
                raise ValueError()


def parse_property_directory(body: bytes, latest_quarter: str, *, max_candidates: int, quarter_fallbacks: int) -> dict:
    """仅检查原目录内当季及允许的一季回退；不请求/推导任何链接。"""
    try:
        year, number, _ = _quarter(latest_quarter)
        if type(max_candidates) is not int or not 1 <= max_candidates <= 2 or type(quarter_fallbacks) is not int or not 0 <= quarter_fallbacks <= 1:
            raise ValueError()
        _document(body)
        parser = _DirectoryLinks()
        parser.feed(_decode(body))
        parser.close()
        if parser.href is not None:
            raise ValueError()
        selected = {}
        for href, label in parser.links:
            quarter = _report_quarter(label)
            if quarter is None:
                continue
            source_year, source_number, _ = _quarter(quarter)
            distance = year * 4 + number - source_year * 4 - source_number
            if not 0 <= distance <= quarter_fallbacks:
                continue
            # 校验原始路径，防止 urljoin 把 ../ 的证据静默清洗成合法路径。
            if '..' in href or '%' in href or '\\' in href:
                raise ValueError()
            url = urljoin('https://www.pbc.gov.cn/diaochatongjisi/116219/116225/index.html', href)
            if re.fullmatch(r'https://www\.pbc\.gov\.cn/diaochatongjisi/116219/116225/[0-9]{17,22}/index\.html', url) is None:
                raise ValueError()
            if quarter in selected and selected[quarter] != url:
                raise ValueError()
            selected[quarter] = url
        if len(selected) > max_candidates:
            raise ValueError()
        chosen = max(selected) if selected else None
        return {'schemaVersion': '1.0', 'role': 'property-directory', 'source': 'pboc',
                'rawSha256': sha256(body).hexdigest(), 'latestQuarter': latest_quarter,
                'candidateCount': len(selected),
                'selected': {'quarter': chosen, 'url': selected[chosen]} if chosen else None}
    except Exception:
        raise ValueError('OFFICIAL_PROPERTY_DIRECTORY_INVALID') from None
