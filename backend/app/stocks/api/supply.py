"""供公告采集及宏观审计读取股票数据供给配置。"""
import json

from app.stocks import financial_supply


def announcement_settings():
    return json.loads(financial_supply.CONFIG_FILE.read_text(encoding="utf-8"))["announcementIndex"]


def financial_collection_enabled():
    return financial_supply.config()["enabled"]
