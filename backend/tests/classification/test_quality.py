from dataclasses import replace

from app.db.models import Classification
from app.marts.source_quality import evaluate_source_quality
from backend.tests.marts.test_screener_builder import _core_snapshot


def test_other_version_parent_cannot_satisfy_publish_quality():
    old = Classification(id=1, provider="tushare", source="SW2014", classification_id="root", name="old", level=1)
    child = Classification(id=2, provider="tushare", source="SW2021", classification_id="child", parent_id="root", name="child", level=2)
    result = {row.name: row for row in evaluate_source_quality(replace(_core_snapshot(), classifications=(old, child)))}
    assert not result["source-classification-valid"].passed
    assert not result["source-classification-coverage"].passed


def test_two_valid_versions_do_not_cross_resolve_parent_levels():
    nodes = (
        Classification(id=1, provider="tushare", source="SW2021", classification_id="root", name="new", level=1),
        Classification(id=2, provider="tushare", source="SW2021", classification_id="child", name="child", level=2, parent_id="root"),
        Classification(id=3, provider="tushare", source="SW2014", classification_id="root", name="old", level=2, parent_id="oldroot"),
        Classification(id=4, provider="tushare", source="SW2014", classification_id="oldroot", name="old root", level=1),
    )
    result = {row.name: row for row in evaluate_source_quality(replace(_core_snapshot(), classifications=nodes))}
    assert result["source-classification-valid"].passed


def test_root_zero_parent_is_valid_but_skipped_parent_level_is_blocked():
    root = Classification(id=1, provider="tushare", source="SW2021", classification_id="root", name="root", level=1, parent_id="0")
    child = Classification(id=2, provider="tushare", source="SW2021", classification_id="child", name="child", level=2, parent_id="root")
    result = {row.name: row for row in evaluate_source_quality(replace(_core_snapshot(), classifications=(root, child)))}
    assert result["source-classification-valid"].passed
    child.level = 3
    result = {row.name: row for row in evaluate_source_quality(replace(_core_snapshot(), classifications=(root, child)))}
    assert not result["source-classification-valid"].passed
