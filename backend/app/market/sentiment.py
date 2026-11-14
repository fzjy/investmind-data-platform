"""日线阈值估算；等级与无成交排除规则为本项目约定。"""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal

from pydantic import Field, field_validator, model_validator
from app.market.data.contracts import StrictModel


class SentimentLevel(StrictModel):
    minimum: int = Field(strict=True, ge=0, le=100)
    label: str = Field(strict=True, min_length=1, max_length=16)


class SentimentOptions(StrictModel):
    threshold: Decimal = Field(default=Decimal('.098'), gt=0, lt=1, allow_inf_nan=False)
    rise_weight: Decimal = Field(default=Decimal('.60'), ge=0, le=1, allow_inf_nan=False)
    threshold_weight: Decimal = Field(default=Decimal('.30'), ge=0, le=1, allow_inf_nan=False)
    retreat_weight: Decimal = Field(default=Decimal('.10'), ge=0, le=1, allow_inf_nan=False)
    rise_normalization: Decimal = Field(default=Decimal('.05'), gt=0, le=1, allow_inf_nan=False)
    retreat_normalization: Decimal = Field(default=Decimal('.50'), gt=0, le=1, allow_inf_nan=False)
    levels: tuple[SentimentLevel, ...] = tuple(SentimentLevel(minimum=n, label=label) for n, label in
        ((0, '极度恐慌'), (20, '恐慌'), (40, '中性'), (60, '贪婪'), (80, '极度贪婪')))

    @field_validator('threshold', 'rise_weight', 'threshold_weight', 'retreat_weight', 'rise_normalization', 'retreat_normalization', mode='before')
    @classmethod
    def reject_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError('SENTIMENT_BOOLEAN_NUMBER')
        return value

    @model_validator(mode='after')
    def check_policy(self):
        if self.rise_weight + self.threshold_weight + self.retreat_weight != 1:
            raise ValueError('SENTIMENT_WEIGHTS_MUST_SUM_TO_ONE')
        minima = [level.minimum for level in self.levels]
        if not minima or minima[0] != 0 or minima != sorted(set(minima)):
            raise ValueError('SENTIMENT_LEVELS_INVALID')
        return self


class SentimentCounts(StrictModel):
    total: int = Field(strict=True, ge=0)
    rise: int = Field(strict=True, ge=0)
    threshold_rise: int = Field(strict=True, ge=0)
    threshold_fall: int = Field(strict=True, ge=0)
    touched: int = Field(strict=True, ge=0)
    retreat: int = Field(strict=True, ge=0)


class SentimentCoverage(StrictModel):
    expected: int = Field(strict=True, ge=0)
    valid: int = Field(strict=True, ge=0)
    excluded: int = Field(strict=True, ge=0)
    missing: int = Field(strict=True, ge=0)


class SentimentContributions(StrictModel):
    rise: float | None = Field(ge=0, le=100, allow_inf_nan=False)
    threshold_rise: float | None = Field(ge=0, le=100, allow_inf_nan=False)
    retreat: float | None = Field(ge=0, le=100, allow_inf_nan=False)


class SentimentSnapshot(StrictModel):
    algorithm: Literal['threshold-v1']
    trade_date: str
    score: int | None = Field(strict=True, ge=0, le=100)
    level: str
    level_basis: str
    formula: str
    status: Literal['complete', 'partial', 'missing', 'unobservable']
    reason: str
    policy: SentimentOptions
    counts: SentimentCounts
    coverage: SentimentCoverage
    rise_ratio: float | None = Field(ge=0, le=1, allow_inf_nan=False)
    threshold_rise_ratio: float | None = Field(ge=0, le=1, allow_inf_nan=False)
    threshold_retreat_ratio: float | None = Field(ge=0, le=1, allow_inf_nan=False)
    contributions: SentimentContributions

    @model_validator(mode='after')
    def check_consistency(self):
        c, v = self.counts, self.coverage
        if (c.total != v.valid or v.expected != v.valid + v.excluded + v.missing
            or not 0 <= c.threshold_rise <= c.rise <= c.total
            or c.threshold_fall > c.total - c.rise
            or not c.threshold_rise <= c.touched <= c.total or c.retreat != c.touched - c.threshold_rise):
            raise ValueError('SENTIMENT_COUNTS_INVALID')
        expected = calculate(c, v, self.policy)
        for key, value in expected.items():
            actual = getattr(self, key)
            if key == 'contributions':
                actual = actual.model_dump()
            if actual != value:
                raise ValueError('SENTIMENT_CALCULATION_MISMATCH')
        return self


def calculate(c, coverage, policy):
    rise = Decimal(c.rise) / c.total if c.total else None
    threshold = Decimal(c.threshold_rise) / c.total if c.total else None
    retreat = Decimal(c.retreat) / c.touched if c.touched else None
    parts = (100 * policy.rise_weight * rise if rise is not None else None,
             100 * policy.threshold_weight * min(threshold / policy.rise_normalization, 1) if threshold is not None else None,
             100 * policy.retreat_weight * (1 - min(retreat / policy.retreat_normalization, 1)) if retreat is not None else None)
    status = 'partial' if coverage.missing else 'missing' if not c.total else 'unobservable' if not c.touched else 'complete'
    score = int(sum(parts).quantize(Decimal(1), rounding=ROUND_HALF_UP)) if status == 'complete' else None
    return dict(score=score, status=status,
        level=next(level.label for level in reversed(policy.levels) if score >= level.minimum) if score is not None else '未计算',
        rise_ratio=float(rise) if rise is not None else None,
        threshold_rise_ratio=float(threshold) if threshold is not None else None,
        threshold_retreat_ratio=float(retreat) if retreat is not None else None,
        contributions=dict(zip(('rise', 'threshold_rise', 'retreat'), (round(float(p), 4) if p is not None else None for p in parts))))


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def build_sentiment(prices, securities, trade_date, policy):
    securities = {row.ts_code: row for row in securities}
    grouped = {}
    for row in prices:
        if row.trade_date == trade_date:
            grouped.setdefault(row.ts_code, []).append(row)
    symbols = set(securities) | set(grouped)
    counts = dict(total=0, rise=0, threshold_rise=0, threshold_fall=0, touched=0, retreat=0)
    excluded = missing = 0
    for symbol in symbols:
        rows = grouped.get(symbol, [])
        if len(rows) != 1:
            missing += 1
            continue
        row = rows[0]
        volume = _decimal(row.vol)
        security = securities.get(symbol)
        if volume == 0 or (security is not None and getattr(security, 'list_date', None) == trade_date):
            excluded += 1
            continue
        close, previous, high = (_decimal(getattr(row, key, None)) for key in ('close', 'pre_close', 'high'))
        if (any(v is None or v <= 0 for v in (close, previous, high, volume)) or high < close):
            missing += 1
            continue
        # 比较价格乘积，避免9.8%边界的浮点误差。
        up = close >= previous * (1 + policy.threshold)
        touched = high >= previous * (1 + policy.threshold)
        counts['total'] += 1
        counts['rise'] += int(close > previous)
        counts['threshold_rise'] += int(up)
        counts['threshold_fall'] += int(close <= previous * (1 - policy.threshold))
        counts['touched'] += int(touched)
        counts['retreat'] += int(touched and not up)
    c = SentimentCounts(**counts)
    coverage = SentimentCoverage(expected=len(symbols), valid=c.total, excluded=excluded, missing=missing)
    values = calculate(c, coverage, policy)
    reasons = {'complete': '同日覆盖样本阈值估算，非交易所真实涨跌停统计。',
        'partial': '必要行情缺失或无效，覆盖不足，不计算总分。',
        'missing': '无合格行情，不计算总分。',
        'unobservable': '当日未触及上涨阈值，回落项不可观测，不计算总分。'}
    pct = lambda value: format(value * 100, 'g')
    formula = (f"100 × [{pct(policy.rise_weight)}% × 上涨占比 + {pct(policy.threshold_weight)}% × min(阈值上涨占比 / {pct(policy.rise_normalization)}%, 1)"
               f" + {pct(policy.retreat_weight)}% × (1 − min(阈值回落率 / {pct(policy.retreat_normalization)}%, 1))]")
    return SentimentSnapshot(algorithm='threshold-v1', trade_date=trade_date.isoformat(), policy=policy,
        counts=c, coverage=coverage, formula=formula, reason=reasons[values['status']],
        level_basis='等级为本项目约定，非原站核实；总分四舍五入后分档。无成交及已知上市首日排除。',
        **values).model_dump(mode='json', by_alias=True)
