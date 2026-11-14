from __future__ import annotations

from app.shared.validation import require_object


PORTFOLIO_KEYS = {"assets", "prices", "snapshotLabel"}


def normalize_portfolio(value: object) -> dict[str, object]:
    data = require_object(value, PORTFOLIO_KEYS)
    data.pop("defaultState", None)
    data.pop("storageKey", None)
    return data


def legacy_portfolio(value: object, source: str) -> dict[str, object]:
    """旧 GET 的冻结展示合同。新发布只存公共输入。"""
    data = require_object(value, PORTFOLIO_KEYS)
    defaults = {
        "period": "1y" if source == "fixture" else "3y", "positions": [],
        "schemaVersion": 1, "strategy": "buy-and-hold", "weightMode": "manual",
    }
    benchmarks = data.get("benchmarks")
    previous = data.get("defaultState")
    if not isinstance(previous, dict) and isinstance(benchmarks, dict) and {"H00300.CSI", "H00922.CSI"} <= benchmarks.keys():
        defaults.update(benchmarkAssetId="H00300.CSI", secondaryBenchmarkAssetId="H00922.CSI")
    if isinstance(previous, dict):
        # 历史公共模板只能传播展示选项，任何用户持仓与账户设置均不回传。
        for key in ("period", "strategy", "weightMode", "schemaVersion", "benchmarkAssetId", "secondaryBenchmarkAssetId"):
            if key in previous:
                defaults[key] = previous[key]
    data["defaultState"] = defaults
    data.setdefault("storageKey", "danginvest:portfolio-sandbox:v1")
    return data
