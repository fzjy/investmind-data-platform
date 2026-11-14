"""新增门禁的真实 PostgreSQL 旧发布保护，使用登记的独立容器。"""
from copy import deepcopy
from dataclasses import replace

import pytest

from app.marts.publication import QualityCheckFailed, SnapshotPublisher
from backend.tests.marts.test_finite_quality import candidates, checks
from backend.tests.marts.test_publication_postgres import postgres_factory, pytestmark
from backend.tests.marts.test_row_consistency import assert_blocked, stored_publication


@pytest.mark.parametrize("failure", ["screener", "workbench", "chart"])
def test_postgres_real_consistency_gate_retains_pages_pointers_and_structured_rows(postgres_factory, failure):
    publisher = SnapshotPublisher(postgres_factory)
    original = candidates()
    quality = checks(original)
    assert all(result.passed for result in quality if result.severity == "blocking")
    version = f"consistency-good-{failure}"
    publisher.publish_many(original, version, quality)
    before = stored_publication(postgres_factory)
    screener, workbench = original
    if failure == "screener":
        row = {**screener.rows.screener[0], "price": 1499}
        screener = replace(screener, rows=replace(screener.rows, screener=(row,)))
    elif failure == "workbench":
        row = deepcopy(workbench.rows.workbenches[0])
        row["quote"]["price"] = 1499
        workbench = replace(workbench, rows=replace(workbench.rows, workbenches=(row,)))
    else:
        row = {**workbench.rows.charts[0], "volume": 101}
        workbench = replace(workbench, rows=replace(workbench.rows, charts=(row,)))
    invalid = (screener, workbench)
    assert_blocked(invalid)
    with pytest.raises(QualityCheckFailed):
        publisher.publish_many(invalid, f"consistency-bad-{failure}", checks(invalid))
    assert stored_publication(postgres_factory) == before
    assert publisher.current("screener").version == version
    assert publisher.current("stock-workbench:600519.SH").version == version
