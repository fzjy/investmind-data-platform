from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta

from app.ops.backup.contracts import BackupManifest


def plan_retention(
    packages: Sequence[BackupManifest], *, verified_ids: frozenset[str], now: datetime,
    keep_count: int, retention_days: int,
) -> tuple[str, ...]:
    groups: dict[str, list[BackupManifest]] = defaultdict(list)
    for package in packages:
        groups[package.source_identity].append(package)
    candidates: list[str] = []
    for identity in sorted(groups):
        ordered = sorted(groups[identity], key=lambda package: (package.completed_at, package.backup_id))
        verified = {package.backup_id for package in ordered if package.backup_id in verified_ids}
        if len(ordered) <= keep_count or len(verified) < 2:
            continue
        latest_verified = next(package.backup_id for package in reversed(ordered) if package.backup_id in verified)
        protected = {package.backup_id for package in ordered[-keep_count:]} | {ordered[-1].backup_id, latest_verified}
        for package in ordered:
            if package.backup_id in protected or now - package.completed_at <= timedelta(days=retention_days):
                continue
            if package.backup_id in verified and len(verified) <= 2:
                continue
            candidates.append(package.backup_id)
            verified.discard(package.backup_id)
    return tuple(candidates)
