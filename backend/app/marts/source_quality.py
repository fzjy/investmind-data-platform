"""快照源证据检查；未知不冒充确定错误，也不伪造来源覆盖。"""
from datetime import date

from app.marts.contracts import QualityResult
from app.shared.stock_mapping import classification_source
from app.shared.calendar import calendar_exchange


def evaluate_source_quality(snapshot) -> tuple[QualityResult, ...]:
    securities = {getattr(row, "ts_code", None): row for row in snapshot.securities
                  if isinstance(getattr(row, "ts_code", None), str)}
    calendars = {}
    for row in getattr(snapshot, "trade_calendar", ()):
        calendars.setdefault((row.exchange, row.cal_date), []).append(row.is_open)
    market_issues, missing_calendar = [], set()
    exchanges = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}
    for collection in ("daily_prices", "daily_basics", "adjustment_factors"):
        for row in getattr(snapshot, collection, ()):
            security = securities.get(row.ts_code)
            if security is None:
                market_issues.append({"collection": collection, "symbol": row.ts_code, "reason": "unknown-security"})
                continue
            if not isinstance(row.trade_date, date) or row.trade_date > snapshot.as_of:
                market_issues.append({"collection": collection, "symbol": row.ts_code, "reason": "invalid-date"})
                continue
            exchange = security.exchange or exchanges.get(row.ts_code.rsplit(".", 1)[-1])
            if exchange == 'BSE':
                exchange = calendar_exchange(exchange, {key[0] for key in calendars})
            key = (exchange, row.trade_date)
            states = calendars.get(key, [])
            if not states:
                missing_calendar.add(f"{exchange}:{row.trade_date.isoformat()}")
            elif len(states) != 1 or states[0] is not True:
                market_issues.append({"collection": collection, "symbol": row.ts_code,
                                      "date": row.trade_date.isoformat(), "reason": "closed-or-conflicting-calendar"})

    financial_issues = []
    missing_disclosures = getattr(snapshot, "missing_financial_disclosure_count", 0)
    for index, point in enumerate(getattr(snapshot, "financial_points", ())):
        if point.visible_at is None:
            missing_disclosures += 1
            continue
        if (not isinstance(point.end_date, date) or not isinstance(point.visible_at, date)
                or point.end_date > point.visible_at or point.visible_at > snapshot.as_of):
            financial_issues.append({"index": index, "reason": "report-disclosure-order"})

    classifications = getattr(snapshot, "classifications", ())
    classes = {row.id: row for row in classifications}
    by_key = {(row.provider, classification_source(row), row.classification_id): row for row in classifications}
    aliases = {}
    for row in classifications:
        key = (row.provider, classification_source(row), str((row.data or {}).get("industry_code")))
        if key[-1] not in ("None", ""):
            aliases.setdefault(key, []).append(row)
    def parent_node(row):
        key = (row.provider, classification_source(row), row.parent_id)
        direct = by_key.get(key)
        matches = aliases.get(key, [])
        if direct is not None and matches and any(node.id != direct.id for node in matches):
            return None
        return direct or (matches[0] if len(matches) == 1 else None)
    class_issues, missing_parents = [], []
    for row in classifications:
        has_parent = row.parent_id not in (None, "", "0", 0)
        parent = parent_node(row) if has_parent else None
        if (has_parent and parent is None) or (not has_parent and row.level in (2, 3)):
            missing_parents.append(f"{row.provider}:{classification_source(row)}:{row.classification_id}")
            class_issues.append({"classId": row.id, "reason": "missing-version-parent"})
        elif parent is not None and row.level is not None and parent.level is not None and parent.level != row.level - 1:
            class_issues.append({"classId": row.id, "reason": "parent-level"})
        seen = set()
        current = row
        while current is not None:
            key = (current.provider, classification_source(current), current.classification_id)
            if key in seen:
                class_issues.append({"classId": row.id, "reason": "cycle"})
                break
            seen.add(key)
            current = parent_node(current) if current.parent_id not in (None, "", "0", 0) else None
    for index, member in enumerate(getattr(snapshot, "classification_history", ())):
        if member.effective_to is not None and member.effective_from > member.effective_to:
            class_issues.append({"index": index, "reason": "inverted-interval"})
    for index, member in enumerate(getattr(snapshot, "classification_members", ())):
        if (member.classification_id not in classes or member.ts_code not in securities
                or member.effective_from > snapshot.as_of
                or member.effective_to is not None and (member.effective_to < member.effective_from or member.effective_to < snapshot.as_of)):
            class_issues.append({"index": index, "reason": "invalid-visible-member"})

    return (
        QualityResult("source-trading-days", "blocking", not market_issues, {"issues": market_issues}),
        QualityResult("source-calendar-coverage", "advisory", not missing_calendar,
                      {"missingCount": len(missing_calendar), "keys": sorted(missing_calendar)}),
        QualityResult("source-financial-dates", "blocking", not financial_issues, {"issues": financial_issues}),
        QualityResult("source-financial-coverage", "advisory", missing_disclosures == 0, {"missingDisclosureCount": missing_disclosures}),
        QualityResult("source-classification-valid", "blocking", not class_issues, {"issues": class_issues}),
        QualityResult("source-classification-coverage", "advisory", not missing_parents, {"missingParents": sorted(missing_parents)}),
    )
