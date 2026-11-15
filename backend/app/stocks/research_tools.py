"""已发布池的只读比较与同业价格比；不冒充原站未公开的诊断算法。"""
from datetime import date
from decimal import Decimal, DecimalException, localcontext
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config_paths import CONFIG_ROOT
from app.shared.contracts import success_result
from app.shared.errors import DataServiceError
from app.shared.validation import normalize_stock_symbol
from app.stocks.bank_scoring import number
from app.stocks.normalizer import normalize_screener_universe


CONFIG_FILE = CONFIG_ROOT / "stocks/financial-indicators.json"


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, populate_by_name=True)
    symbol: str
    peer_symbol: str | None = Field(default=None, alias='peerSymbol')

    @field_validator('symbol', 'peer_symbol')
    @classmethod
    def valid_symbol(cls, value):
        return normalize_stock_symbol(value) if value is not None else None

    @model_validator(mode='after')
    def distinct(self):
        if self.symbol == self.peer_symbol:
            raise ValueError('不能与自身配对')
        return self


def _finite(value):
    exact = number(value)
    return float(exact) if exact is not None else None


def _compute(fn):
    try:
        with localcontext() as context:
            context.prec = 50
            return _finite(fn())
    except (DecimalException, OverflowError, ZeroDivisionError, ValueError):
        return None


def _identity(stock, rules):
    # 已登记A股daily/daily_basic的人民币币种合同；未知板块不根据名称猜币种。
    currency = stock.get('currency') or rules['currencyByBoard'].get(stock.get('board'))
    return dict(symbol=stock['symbol'], name=str(stock.get('name') or stock['symbol']),
                industry=str(stock.get('majorIndustry') or stock.get('industry') or ''), currency=currency)


def _value(stock, definition):
    if stock is None:
        return None
    source = stock.get('researchValues') if definition['qualified'] else stock
    value = number(source.get(definition['key'])) if isinstance(source, dict) else None
    if value is not None and definition.get('positive') and value <= 0:
        return None
    return value


def _statistics(value, values, minimum):
    if value is None or len(values) < minimum:
        return None, None
    percentile = _compute(lambda: Decimal(sum(x <= value for x in values)) * 100 / len(values))

    def z_score():
        mean = sum(values) / len(values)
        variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
        return (value - mean) / variance.sqrt() if variance > 0 else Decimal('NaN')

    return percentile, _compute(z_score)


def _closes(raw, cutoff):
    points = {}
    chart = raw.get('dailyChart')
    rows = chart.get('candles') if isinstance(chart, dict) else None
    if not isinstance(rows, list):
        return {}, '缺少已发布日线'
    for row in rows:
        if not isinstance(row, dict):
            return {}, '日线结构无效'
        day = row.get('date')
        try:
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                return {}, '日线日期无效'
        except ValueError:
            return {}, '日线日期无效'
        if day > cutoff:
            continue
        value = number(row.get('close'))
        if value is None or value <= 0:
            return {}, '存在无法核验的日线收盘价'
        if day in points and points[day] != value:
            return {}, '同日收盘价存在冲突'
        points[day] = value
    return points, ''


def _pair(provider, snapshot, current, peer):
    result = dict(status='unavailable', reason='', currency=current['currency'],
                  peerSymbol=peer['symbol'] if peer else None, ratio=None, asOf=None, points=[],
                  basis='published-chart-close-ratio')
    if peer is None:
        return result | {'reason': '当前池没有可推荐同行，请选择另一证券'}
    if not current['currency'] or current['currency'] != peer['currency']:
        return result | {'reason': '两腿币种缺失或不同，未建立汇率合同'}
    histories = []
    for identity in (current, peer):
        try:
            leg = provider.read_workbench(identity['symbol'])
        except DataServiceError as error:
            if error.code in ('DATA_NOT_FOUND', 'DATA_NOT_READY'):
                return result | {'reason': '配对证券缺少已发布工作台日线'}
            raise
        if any(getattr(leg.meta, k) != getattr(snapshot.meta, k) for k in ('source', 'version', 'as_of')):
            return result | {'reason': '配对工作台与选股发布版本、来源或业务日期不一致'}
        raw = leg.raw
        if not isinstance(raw, dict) or raw.get('symbol') != identity['symbol'] or raw.get('isFallbackSample') is not False:
            return result | {'reason': '配对证券身份缺失或使用回退样本'}
        quote = raw.get('quote')
        if not isinstance(quote, dict) or quote.get('symbol') != identity['symbol']:
            return result | {'reason': '配对报价证券身份不符'}
        prices, reason = _closes(raw, snapshot.meta.as_of[:10])
        if reason:
            return result | {'reason': reason}
        histories.append(prices)
    left, right = histories
    for day in sorted(left.keys() & right.keys()):
        value = _compute(lambda: left[day] / right[day])
        if value is None or value <= 0:
            return result | {'reason': '价格比溢出或无法表示，未生成部分结果'}
        result['points'].append(dict(date=day, value=value))
    if not result['points']:
        return result | {'reason': '没有同一交易日的有效双腿价格'}
    last = result['points'][-1]
    return result | dict(status='available', reason='沿已发布K线的同日价格比；复权完整性未核验，不代表收益率、全收益或A/H溢价',
                         ratio=last['value'], asOf=last['date'])


class ResearchToolsService:
    def __init__(self, provider):
        self.provider = provider

    def query(self, request: ResearchRequest):
        rules = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))['researchTools']
        snapshot = self.provider.read_screener_universe()
        stocks = normalize_screener_universe(snapshot.raw)['stocks']
        indexed = {}
        for stock in stocks:
            symbol = stock.get('symbol')
            if not isinstance(symbol, str) or symbol in indexed:
                raise DataServiceError('INVALID_SOURCE_PAYLOAD', '发布池证券身份缺失或重复')
            indexed[symbol] = stock
        if request.symbol not in indexed or (request.peer_symbol is not None and request.peer_symbol not in indexed):
            raise DataServiceError('DATA_NOT_FOUND', '证券不在当前已发布股票池中')
        current = indexed[request.symbol]
        identity = _identity(current, rules)
        peer = indexed.get(request.peer_symbol)
        if peer is None and identity['industry'] and identity['currency']:
            same = [s for s in stocks if s['symbol'] != request.symbol
                    and _identity(s, rules)['industry'] == identity['industry']
                    and _identity(s, rules)['currency'] == identity['currency']
                    and number(s.get('marketCap')) is not None and number(s.get('marketCap')) > 0]
            same.sort(key=lambda s: (-number(s['marketCap']), s['symbol']))
            peer = same[0] if same else None
        peer_identity = _identity(peer, rules) if peer else None
        metrics = []
        for definition in rules['metrics']:
            value, other = _value(current, definition), _value(peer, definition)
            amount = definition['key'] in ('marketCap', 'netCash')
            same_currency = bool(identity['currency']) and peer_identity is not None and peer_identity['currency'] == identity['currency']
            eligible = [s for s in stocks if identity['currency'] and _identity(s, rules)['currency'] == identity['currency']] if amount else stocks
            if amount:
                if not identity['currency']:
                    value = None
                if not same_currency:
                    other = None
            values = [_value(s, definition) for s in eligible]
            values = [v for v in values if v is not None]
            industry_values = [_value(s, definition) for s in eligible
                               if identity['industry'] and _identity(s, rules)['industry'] == identity['industry']]
            industry_values = [v for v in industry_values if v is not None]
            percentile, z_score = _statistics(value, values, rules['minimumSampleCount'])
            industry_percentile, _ = _statistics(value, industry_values, rules['minimumSampleCount'])
            difference = _compute(lambda: value - other) if value is not None and other is not None else None
            reason = ('缺少合格输入，不使用旧参考或模拟数值' if value is None else
                      '有效样本不足，不计算分位或标准分' if len(values) < rules['minimumSampleCount'] else
                      '发布值样本比较；分位为小于等于本值占比，不表示投资优劣')
            if amount:
                reason += ('；金额分布仅含与主股相同币种' + str(identity['currency']) + '的有效样本' if identity['currency'] else '；主股金额币种未知，不计算金额分布')
                if peer_identity is not None and not same_currency:
                    reason += '；副股币种不同或未知，没有汇率合同，副股金额和差值不可比'
            metrics.append({k: definition[k] for k in ('key', 'label', 'unit', 'direction')} |
                           dict(value=_finite(value), peerValue=_finite(other), difference=difference,
                                percentile=percentile, industryPercentile=industry_percentile,
                                sampleCount=len(values), industrySampleCount=len(industry_values),
                                zScore=z_score, reason=reason))
        result = dict(algorithmVersion=rules['algorithmVersion'],
                      scope=dict(kind='published-pool', universeCount=len(stocks), asOf=snapshot.meta.as_of, version=snapshot.meta.version),
                      current=identity, peer=peer_identity,
                      instruments=sorted((_identity(s, rules) for s in stocks), key=lambda x: x['symbol']),
                      metrics=metrics, pair=_pair(self.provider, snapshot, identity, peer_identity),
                      limitations=['仅当前已发布池，不代表全市场；发布截止不保证所有财务值属于同一报告期。',
                                   '比较仅支持当前发布版本；未重建历史时点或未发布ETF数据。',
                                   '价格比沿已发布K线口径，复权完整性未核验；历史长度按实际供给，不承诺10年。',
                                   '原站历史形态匹配、智能诊断及全市场异常画像算法尚未恢复，不生成自拟匹配分数。'])
        return success_result(result, snapshot.meta)
