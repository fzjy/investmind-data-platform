"""发布价格快照上的只读买入持有分析；收益率为小数，公共 Sharpe 的 Rf=0。"""
from __future__ import annotations

from datetime import date
import json
import math
from pathlib import Path
import re
from statistics import pstdev
from typing import NoReturn

from app.shared.errors import DataServiceError


PORTFOLIO_SETTINGS_FILE = Path(__file__).resolve().parents[3] / "config" / "portfolio.json"
# 允许共同上市锚点 1900-01-01，同时限制异常请求跨度；不生成日历。
MAX_WINDOW_DAYS = 200 * 366
METRIC_KEYS = ("totalReturn", "annualReturn", "annualVolatility", "sharpe", "maximumDrawdown")


def invalid_request(message: str) -> NoReturn:
    error = DataServiceError("INVALID_REQUEST", message)
    error.status = 422
    raise error


def _iso_date(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("日期必须为 YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def _number(value: object) -> bool:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _validated_settings(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise DataServiceError("INTERNAL_ERROR", "组合共享配置必须为对象")
    for key in ("annualizationDays", "apiMaxAssets"):
        number = value.get(key)
        if type(number) is not int or not _number(number) or number < 1:
            raise DataServiceError("INTERNAL_ERROR", f"组合共享配置 {key} 必须为正有限整数")
    if value["annualizationDays"] > 366:
        raise DataServiceError("INTERNAL_ERROR", "组合共享配置 annualizationDays 不得超过 366")
    if value["apiMaxAssets"] > 10000:
        raise DataServiceError("INTERNAL_ERROR", "组合共享配置 apiMaxAssets 不得超过 10000")
    return {key: value[key] for key in ("annualizationDays", "apiMaxAssets")}


def load_analysis_settings() -> dict[str, int]:
    """每次请求读取同一根 JSON；apiMaxAssets 是 API 性能保护，不是证券池数量。"""
    try:
        value = json.loads(PORTFOLIO_SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DataServiceError("INTERNAL_ERROR", "无法读取组合共享配置") from None
    return _validated_settings(value)


def validate_request(value: object, settings: dict[str, int] | None = None) -> dict:
    active_settings = load_analysis_settings() if settings is None else _validated_settings(settings)
    max_holdings = active_settings["apiMaxAssets"]
    if not isinstance(value, dict) or set(value) - {"holdings", "startDate", "endDate", "benchmarkAssetId"}:
        invalid_request("分析请求必须为对象且仅含支持的字段")
    holdings = value.get("holdings")
    if not isinstance(holdings, list) or not 1 <= len(holdings) <= max_holdings:
        invalid_request(f"持仓数量须为 1 至 {max_holdings}（API 性能保护，非证券池数量）")
    seen: set[str] = set()
    for holding in holdings:
        if not isinstance(holding, dict) or set(holding) != {"assetId", "weight"}:
            invalid_request("持仓须包含 assetId 与 weight")
        asset_id, weight = holding["assetId"], holding["weight"]
        if not isinstance(asset_id, str) or not asset_id or len(asset_id) > 128 or asset_id in seen:
            invalid_request("assetId 不能为空、过长或重复")
        if not _number(weight) or weight < 0:
            invalid_request("weight 必须为非负有限数值")
        seen.add(asset_id)
    if not any(h["weight"] > 0 for h in holdings):
        invalid_request("至少一个持仓的 weight 必须大于 0")
    for key in ("startDate", "endDate"):
        if key in value:
            try:
                _iso_date(value[key])
            except ValueError:
                invalid_request(f"{key} 必须为有效的 YYYY-MM-DD 日期")
    if "benchmarkAssetId" in value and (
        not isinstance(value["benchmarkAssetId"], str) or not 1 <= len(value["benchmarkAssetId"]) <= 128
    ):
        invalid_request("benchmarkAssetId 必须为非空字符串")
    if "startDate" in value and "endDate" in value:
        _validate_window(value["startDate"], value["endDate"])
    return value


def _validate_window(start: str, end: str) -> None:
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    if days < 0 or days > MAX_WINDOW_DAYS:
        invalid_request("日期区间倒置或超过 200 年保护上限")


def _prices(rows: object) -> tuple[dict[str, float], set[str], bool]:
    valid: dict[str, float] = {}
    calendar: set[str] = set()
    bad = False
    if not isinstance(rows, list):
        return valid, calendar, True
    for row in rows:
        try:
            day = _iso_date(row.get("date") if isinstance(row, dict) else None)
        except ValueError:
            bad = True
            continue
        if day in calendar:
            valid.pop(day, None)
            bad = True
            continue
        calendar.add(day)
        price = row.get("price")
        if _number(price) and price > 0:
            valid[day] = float(price)
        else:
            bad = True
    return valid, calendar, bad


def analyze_portfolio(
    raw: dict[str, object], request: dict, settings: dict[str, int] | None = None,
) -> dict[str, object]:
    """仅消费 normalize_portfolio 输出，不读库、不调用供应商、不修改输入。"""
    active_settings = load_analysis_settings() if settings is None else _validated_settings(settings)
    annualization_days = active_settings["annualizationDays"]
    assets = {a["id"]: a for a in raw["assets"] if isinstance(a, dict) and isinstance(a.get("id"), str)}
    for asset_id in [h["assetId"] for h in request["holdings"]]:
        if asset_id not in assets:
            raise DataServiceError("DATA_NOT_FOUND", f"当前发布快照中没有资产 {asset_id}")
    reference=request.get('benchmarkAssetId')
    if reference and reference not in assets and reference not in raw.get('benchmarks',{}):
        raise DataServiceError('DATA_NOT_FOUND',f'当前发布快照中没有参照 {reference}')
    positive = [h for h in request["holdings"] if h["weight"] > 0]
    scale = max(h["weight"] for h in positive)
    total = math.fsum(h["weight"] / scale for h in positive)
    holdings = [{"assetId": h["assetId"], "weight": h["weight"] / scale / total} for h in positive]
    price_source = raw["prices"]
    series = {key: _prices(price_source.get(key)) for key in assets}
    noncash = [h["assetId"] for h in holdings if assets[h["assetId"]].get("type") != "cash"]
    # 纯现金优先沿快照实际非现金日历；无参考时仅保留已发布现金日期。
    published_references = [key for key, asset in assets.items() if asset.get("type") != "cash"]
    references = noncash or published_references
    calendar = set().union(*(series[key][1] for key in references)) if references else set()
    if not calendar and not noncash:
        calendar = set().union(*(series[h["assetId"]][1] for h in holdings))
    available = sorted(calendar)
    start = request.get("startDate", available[0] if available else request.get("endDate"))
    end = request.get("endDate", available[-1] if available else request.get("startDate"))
    if start and end:
        # 请求与快照无重叠并非非法；只校验用户实际跨度（含省略端点）。
        if "startDate" not in request or "endDate" not in request:
            if abs((date.fromisoformat(end) - date.fromisoformat(start)).days) > MAX_WINDOW_DAYS:
                invalid_request("日期区间超过 200 年保护上限")
    requested_days = [d for d in available if (not start or d >= start) and (not end or d <= end)]
    common = set(requested_days)
    for key in noncash:
        common &= series[key][0].keys()
    days = sorted(common)
    warnings: list[str] = []
    if any(series[key][2] for key in noncash):
        warnings.append("发布价格存在无效值或重复日期；未填充缺价。")
    # 即使只有一个持仓，也用其他已发布非现金日期识别可观测缺口。
    # 不据自然日推断休市日，也不因参考资产覆盖更长而扩大有效窗口。
    reference_calendar = set().union(*(series[key][1] for key in published_references))
    inner_gap = bool(days) and any(days[0] < d < days[-1] and d not in common for d in reference_calendar)
    if inner_gap:
        warnings.append("共同日历存在缺价内洞，禁止跨日当作日收益；年化收益、波动率和夏普不可用，回撤仅反映已观测点。")
    nav: list[dict[str, object]] = []
    for day in days:
        value = math.fsum(h["weight"] * (series[h["assetId"]][0][day] / series[h["assetId"]][0][days[0]]
                         if h["assetId"] in noncash else 1.0) for h in holdings)
        if not math.isfinite(value) or value <= 0:
            warnings.append("价格比例超出有限数值范围，净值及统计不可用。")
            nav = []
            break
        nav.append({"date": day, "value": value})
    metrics = dict.fromkeys(METRIC_KEYS)
    values = [point["value"] for point in nav]
    if len(values) >= 2:
        metrics["totalReturn"] = values[-1] - 1
        peak = values[0]
        drawdown = 0.0
        for value in values:
            peak = max(peak, value)
            drawdown = min(drawdown, value / peak - 1)
        metrics["maximumDrawdown"] = drawdown
        if not inner_gap:
            try:
                metrics["annualReturn"] = math.expm1(math.log(values[-1]) * annualization_days / (len(values) - 1))
            except OverflowError:
                warnings.append("年化收益超出有限数值范围。")
            returns = [b / a - 1 for a, b in zip(values, values[1:])]
            if len(returns) >= 2 and all(math.isfinite(r) for r in returns):
                sigma = pstdev(returns)
                metrics["annualVolatility"] = sigma * math.sqrt(annualization_days)
                if sigma > 0:
                    metrics["sharpe"] = math.fsum(r / len(returns) for r in returns) / sigma * math.sqrt(annualization_days)
                else:
                    warnings.append("日收益为常数，夏普比率无定义（公共 Rf=0）。")
            else:
                warnings.append("不足两个有限日收益，波动率与夏普不可用。")
    else:
        warnings.append("共同价格观测不足两个，无法计算区间收益和风险指标；未生成交易日。")
    for key, value in metrics.items():
        if value is not None and not math.isfinite(value):
            metrics[key] = None
            warnings.append(f"{key} 超出有限数值范围。")
    if request.get("benchmarkAssetId"):
        warnings.append("benchmarkAssetId 已验证属于发布资产或独立指数参照；本接口未输出基准风险统计，前端按同日序列计算并区分价格/全收益口径。")
    reasons = {
        "dividends": "当前发布快照未包含逐笔现金分红、除权/支付日、TTM/NTM 与再投资核验输入；core 的分红结构不等于已发布组合分红读模型。",
        "attribution": "当前发布快照未包含区间财务、估值、股东回报及可对账的财富拆解输入。",
        "indexBenchmarks": "当前发布快照未包含经验证的真实指数价格/全收益序列及口径元数据。",
        "mpt": "当前发布快照未包含优化模型、约束、利率曲线及求解诊断；此接口仅提供买入持有价格统计。",
    }
    if raw.get('dividendLedger'):
        reasons['dividends']='已有可追溯的已观察事件账本；尚缺合格完整覆盖、资本连续性及独立预测输入，TTM/NTM等完整经济指标保持缺失。'
    capabilities={key:{'status':'missing','reason':reason} for key,reason in reasons.items()}
    from app.portfolio.attribution_inputs import valid_attribution_inputs
    stock_ids = [asset_id for asset_id, asset in assets.items() if asset.get('type') == 'stock']
    if valid_attribution_inputs(raw.get('attributionInputs'), stock_ids, raw.get('snapshotLabel')):
        capabilities['attribution'] = {'status':'partial',
            'reason':'已发布历史可见财报与逐日股本。个股透视按所选区间核对盈利、估值及股东回报，缺季或非正盈利逐项说明。'}
    if raw.get('dividendYieldInputs'):
        capabilities['dividends'] = {'status':'partial', 'reason':'组合页按持仓、估值日和逐股覆盖分别计算四项股息率。'}
    from app.portfolio.reference_history import valid_benchmarks
    if raw.get('benchmarks') and valid_benchmarks(raw['benchmarks'],raw.get('snapshotLabel')):
        capabilities['indexBenchmarks']={'status':'available','reason':'基准采用已发布的全收益指数。'}
    return {
        "effectiveWindow": {"startDate": nav[0]["date"] if nav else None,
                            "endDate": nav[-1]["date"] if nav else None, "observations": len(nav)},
        "holdings": holdings, "nav": nav, "metrics": metrics,
        "priceBasis": {"kind": "published-adjusted-price", "totalReturnVerified": False,
                       "notes": ["本接口按发布价格计算。总回报卡根据同版快照中持仓与区间的复权核验记录标明分红再投口径。",
                                 f"静态买入持有，不再平衡，不计手续费/税费；按共享配置 {annualization_days} 个日收益区间年化，总体标准差，公共 Sharpe Rf=0。",
                                 "现金价格为 1，沿用实际非现金参考日历。"]},
        "capabilities": capabilities,
        "warnings": warnings,
    }
