from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest

from app.connectors.cninfo import CninfoAnnouncements
from app.content.announcement_history import Window, preview_pages


def test_preview_stops_at_budget_and_preserves_disclosure_date():
    calls = []

    def query(request):
        form = parse_qs(request.content.decode())
        calls.append(request.url.path)
        if "keyWord" in form:
            return httpx.Response(200, json=[{"code": "688111", "category": "A股", "orgId": "issuer"}])
        assert form["stock"] == ["688111,issuer"] and form["column"] == ["sse"]
        assert form["pageNum"] == ["1"] and form["seDate"] == ["2026-08-20~2026-08-20"]
        return httpx.Response(200, json={"hasMore": True, "totalAnnouncement": 2, "announcements": [{
            "secCode": "688111", "orgId": "issuer", "announcementId": "123", "announcementTitle": "半年报",
            "adjunctUrl": "finalpage/2026-08-20/123.PDF", "announcementTime": 1787155200000,
        }]})

    day = date(2026, 8, 20)
    with httpx.Client(transport=httpx.MockTransport(query)) as client:
        result = preview_pages(Window("688111.SH", day, day), CninfoAnnouncements(client, 1).fetch_page, 1)
    assert result["complete"] is False and result["pages"] == 1 and len(calls) == 2
    assert result["records"] == [{"id": "123", "title": "半年报", "disclosureDate": "2026-08-20",
                                  "sourceUrl": "https://static.cninfo.com.cn/finalpage/2026-08-20/123.PDF"}]


def test_foreign_issuer_in_result_fails_validation():
    def query(request):
        if request.url.path.endswith("topSearch/query"):
            return httpx.Response(200, json=[{"code": "000858", "category": "A股", "orgId": "issuer"}])
        return httpx.Response(200, json={"hasMore": False, "totalAnnouncement": 1,
                                         "announcements": [{"secCode": "000001", "orgId": "other"}]})

    day = date(2026, 8, 20)
    with httpx.Client(transport=httpx.MockTransport(query)) as client, pytest.raises(ValueError, match="不匹配"):
        preview_pages(Window("000858.SZ", day, day), CninfoAnnouncements(client).fetch_page, 1)
