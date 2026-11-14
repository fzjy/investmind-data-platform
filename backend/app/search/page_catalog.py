from dataclasses import dataclass

from app.search.models import SearchPageItem


@dataclass(frozen=True)
class PageEntry:
    id: str
    title: str
    description: str
    href: str
    keywords: tuple[str, ...]


PAGE_CATALOG = (
    PageEntry("home", "首页", "浏览已实现功能入口", "/", ("首页", "主页", "功能入口")),
    PageEntry("market", "市场", "查看市场概览与行情", "/market", ("市场", "行情", "市场概览")),
    PageEntry("macro", "宏观", "查看宏观指标与分析", "/macro", ("宏观", "经济", "宏观分析")),
    PageEntry("screener", "选股器", "筛选当前已有数据的股票", "/stocks?mode=screener", ("选股", "股票筛选", "筛选器")),
)
