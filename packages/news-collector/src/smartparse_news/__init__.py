"""独立新闻采集公共接口；不得依赖宿主应用。"""
from .collector import Collector, Config

__all__ = ["Collector", "Config"]
