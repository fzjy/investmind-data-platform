"""Apply declared rights conservatively; this does not verify a licence."""
from __future__ import annotations

TEXT_STATUSES = frozenset(("licensed", "public_domain"))
DISPLAY_STATUSES = (*sorted(TEXT_STATUSES), "link_only")


def permitted_text(status: str, summary: str | None, body: str | None) -> dict[str, str | None]:
    return {"summary": summary if status in TEXT_STATUSES else None,
            "body": body if status in TEXT_STATUSES else None}
