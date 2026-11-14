"""巨潮公开列表的单证券分页读取。只保留公告索引。"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.connectors.external_files.contracts import ContentImportRecord
from app.content.announcement_history import AnnouncementPage, Window


class CninfoAnnouncements:
    def __init__(self, client: httpx.Client, page_size: int = 30):
        self.client = client
        self.page_size = page_size
        self._org_ids: dict[str, str] = {}
        self.request_count = 0

    def _query(self, path, form):
        self.request_count += 1
        response = self.client.post(f"https://www.cninfo.com.cn/new/{path}", data=form)
        response.raise_for_status()
        return response.json()

    def fetch_page(self, window: Window, cursor: str | None) -> AnnouncementPage:
        if not re.fullmatch(r"(?:6[0-9]{5}\.SH|[03][0-9]{5}\.SZ)", window.symbol):
            raise ValueError("请填写与沪深 A 股市场对应的证券代码及后缀")
        code, exchange = window.symbol.split(".")
        if window.symbol not in self._org_ids:
            matches = self._query("information/topSearch/query", {"keyWord": code, "maxNum": 10})
            if not isinstance(matches, list):
                raise ValueError("巨潮证券目录响应无效")
            ids = {item["orgId"] for item in matches if item.get("code") == code
                   and item.get("category") == "A股" and item.get("orgId")}
            if len(ids) != 1:
                raise ValueError("无法唯一确定巨潮证券标识")
            self._org_ids[window.symbol] = ids.pop()
        number = int(cursor or 1)
        result = self._query("hisAnnouncement/query", {
            "stock": f"{code},{self._org_ids[window.symbol]}", "column": {"SH": "sse", "SZ": "szse"}[exchange],
            "tabName": "fulltext", "seDate": f"{window.start}~{window.end}",
            "pageNum": number, "pageSize": self.page_size, "isHLtitle": "false",
        })
        if not isinstance(result, dict) or type(result.get("hasMore")) is not bool:
            raise ValueError("巨潮公告分页响应无效")
        rows = result.get("announcements")
        total = result.get("totalAnnouncement")
        if rows is None and "announcements" in result and total == 0:
            rows = []
        if not isinstance(rows, list) or type(total) is not int or total < 0:
            raise ValueError("巨潮公告列表缺少记录或总数")
        records = []
        for row in rows:
            identity = row.get("announcementId")
            path = row.get("adjunctUrl", "")
            if (row.get("secCode") != code or row.get("orgId") != self._org_ids[window.symbol]
                    or not isinstance(identity, str) or not re.fullmatch(r"[0-9]+", identity)
                    or not re.fullmatch(rf"finalpage/[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}/{identity}\.pdf", path, re.I)):
                raise ValueError("证券、公告编号或原文地址不匹配")
            # 公开列表的时分口径未经确认，仅保留上海披露日期。
            zone = ZoneInfo("Asia/Shanghai")
            day = datetime.fromtimestamp(row["announcementTime"] / 1000, zone).date()
            records.append(ContentImportRecord(
                schemaVersion="1.0", recordType="content", source="cninfo", contentType="announcement",
                externalId=identity, title=row["announcementTitle"], securities=[window.symbol],
                publishedAt=datetime.combine(day, datetime.min.time(), zone),
                sourceUrl=f"https://static.cninfo.com.cn/{path}", copyrightStatus="link_only",
            ))
        fields = ("announcementId", "secCode", "secName", "orgId", "announcementTitle", "announcementTime",
                  "announcementType", "adjunctUrl", "adjunctType", "replacement", "associateAnnouncement")
        metadata = {"announcements": [{k: row[k] for k in fields if k in row} for row in rows],
                    "totalAnnouncement": total, "hasMore": result["hasMore"]}
        return AnnouncementPage(tuple(records), total, str(number + 1) if result["hasMore"] else None, metadata)
