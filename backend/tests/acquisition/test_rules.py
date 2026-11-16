from datetime import UTC, datetime

import pytest

from app.acquisition.contracts import Assessment, Availability, Evidence
from app.acquisition.rules import classify, classify_derived, summarize

NOW = datetime(2026, 11, 16, tzinfo=UTC)


def value(kind):
    return Availability(kind, None, '无值' if kind == 'missing' else '', None, None)


def execution(status):
    return Evidence(status, NOW, NOW if status == 'success' else None,
                    'REQUEST_FAILED' if status == 'failed' else None, '', {}, ())


@pytest.mark.parametrize(('availability', 'execution_status', 'expected'), [
    ('available', 'success', 'normal'), ('available', 'failed', 'failed'),
    ('confirmed_empty', 'success', 'normal'), ('available', 'unrecorded', 'excluded'),
    ('missing', 'paused', 'excluded'), ('missing', 'running', 'excluded'),
    ('not_applicable', 'success', 'excluded'), ('unknown', 'success', 'excluded'),
])
def test_classification(availability, execution_status, expected):
    assert classify(value(availability), execution(execution_status)).status == expected


def test_missing_has_priority_and_keeps_error():
    result = classify(value('missing'), execution('failed'))
    assert result.status == 'missing'
    assert 'REQUEST_FAILED' in result.reason


def test_excluded_not_in_three_categories():
    assert summarize([Assessment(key, '') for key in ('normal','failed','missing','excluded')]) == {
        'normal': 1, 'failed': 1, 'missing': 1, 'excluded': 1}


def test_dependency_priority_matches_website():
    deps = (Assessment('missing', '输入缺失'), Assessment('failed', '输入失败'))
    assert classify_derived(value('available'), deps).status == 'failed'
    assert classify_derived(value('missing'), deps).status == 'missing'
    assert classify_derived(value('available'), deps[:1]).status == 'missing'
    assert classify_derived(value('available'), ()).status == 'excluded'
