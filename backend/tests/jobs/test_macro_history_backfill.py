from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.connectors.macro.contracts import ProviderFetchResult, ProviderObservation, ProviderTransientError
from app.connectors.macro.nbs_history import NbsReleaseLocator
from app.connectors.macro.nbs_release_feed import INDEX
from app.connectors.macro.transport import TextTransportResponse
from app.db.models import JobRun, MacroObservation, SourcePayload
from app.jobs.cursors import SqlJobCursorRepository
from app.jobs.locks import PostgresAdvisoryJobLock, data_publication_lock_key
from app.macro.jobs.macro_sync_repository import MacroObservationRepository
from app.macro.jobs import macro_history_backfill as backfill
from app.macro.catalog import DEFAULT_MACRO_CATALOG_FILE, MacroSeriesCatalog
from app.macro.history_catalog import ARCHIVED_V7_FILE


AS_OF = datetime(2026, 10, 1, tzinfo=UTC)
URL = INDEX + '202609/t20260909_1965263.html'
TITLE = '2026年8月份居民消费价格同比上涨0.8%'
BODY = f'''<meta name="ArticleTitle" content="{TITLE}"><meta name="PubDate" content="2026/09/09 09:30">
<p>2026年8月份，全国居民消费价格同比上涨0.8%。</p>
<table><tr><td></td><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr>
<tr><td>不包括食品和能源</td><td>0.1</td><td>1.0</td></tr></table>'''
RELEASE = NbsReleaseLocator(series_id='cn.cpi.core_yoy', url=URL, title=TITLE)


def page(number=0, count=1, links=True):
    return (f'<a href="{URL}" title="{TITLE}">cpi</a>' if links else '') + f'createPageHTML({count},{number},"index","html");'


class Transport:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get_text(self, url):
        self.calls.append(url)
        result = self.pages[url]
        if isinstance(result, BaseException):
            raise result
        return TextTransportResponse(result, 1)


@pytest.fixture
def setup(pg_factory):
    engine, factory = pg_factory.kw["bind"], pg_factory
    cursors = SqlJobCursorRepository(factory, job_name='backfill-macro-history')
    catalog = MacroSeriesCatalog.load(ARCHIVED_V7_FILE)
    def job(transport, observations=None):
        return backfill.MacroHistoryBackfillJob(
            catalog=catalog, transport=transport, cursors=cursors,
            observations=observations or MacroObservationRepository(factory),
            history=backfill.SqlMacroHistoryRuns(factory), lock=PostgresAdvisoryJobLock(factory.kw["bind"]), clock=lambda: AS_OF,
        )
    yield factory, cursors, job
    engine.dispose()


def run(job, **changes):
    return job.run(**{'as_of': AS_OF, 'history_years': 1, 'end': date(2026, 8, 31),
                     'request_limit': 2, 'releases': (), **changes})


def test_batch_budget_and_new_instance_resume_without_refetch_done_release(setup):
    factory, cursors, job = setup
    first = Transport({INDEX: page(count=2), URL: BODY})
    report = run(job(first))
    assert report['requestCount'] == 2
    assert report['rowCount'] == 1
    assert report['nextIndexPage'] == 1
    assert report['status'] == 'partial'
    second = Transport({INDEX: page(count=2), INDEX + 'index_1.html': page(1, 2, False)})
    resumed = run(job(second), as_of=datetime(2026, 10, 5, tzinfo=UTC))
    assert resumed['discoveryComplete'] is True
    assert second.calls == [INDEX, INDEX + 'index_1.html']
    assert resumed['asOf'] == '2026-10-01T00:00:00+00:00'
    assert '2026-08' not in resumed['missingMonths']['cn.cpi.core_yoy']
    assert '2026-01' not in resumed['missingMonths']['cn.industry.value_added_yoy']
    assert '2026-02' not in resumed['missingMonths']['cn.industry.value_added_yoy']
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 1
        assert len(session.scalars(select(JobRun).where(JobRun.job_name == 'backfill-macro-history')).all()) == 2


@pytest.mark.parametrize('bad_body', [ProviderTransientError('HTTP_REQUEST_FAILED', request_count=1), '', BODY.replace('2026/09/09', '2026/10/09')])
def test_failed_or_future_report_remains_pending_for_next_batch(setup, bad_body):
    factory, cursors, job = setup
    first = run(job(Transport({URL: bad_body, INDEX: page(links=False)})), releases=(RELEASE,))
    assert first['pendingReleases'] == 1
    assert '2026-08' in first['missingMonths']['cn.cpi.core_yoy']
    second = run(job(Transport({URL: BODY, INDEX: page(links=False)})), releases=(RELEASE,))
    assert second['pendingReleases'] == 0
    assert '2026-08' not in second['missingMonths']['cn.cpi.core_yoy']
    assert second['rowCount'] == 1


def test_crash_after_core_commit_before_cursor_save_is_idempotent(setup, monkeypatch):
    factory, cursors, job = setup
    original = cursors.save
    def interrupted(key, value):
        if any(item['status'] == 'done' for item in value.get('entries', [])):
            raise KeyboardInterrupt()
        original(key, value)
    monkeypatch.setattr(cursors, 'save', interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(job(Transport({URL: BODY})), releases=(RELEASE,))
    monkeypatch.setattr(cursors, 'save', original)
    result = run(job(Transport({URL: BODY, INDEX: page(links=False)})), releases=(RELEASE,))
    assert result['rowCount'] == 0
    assert result['pendingReleases'] == 0
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 1


def test_changed_directory_anchor_restarts_scan_but_keeps_done_reports(setup):
    factory, cursors, job = setup
    run(job(Transport({INDEX: page(count=2), URL: BODY})))
    changed = Transport({INDEX: page(count=3) + '<a href="/unrelated">new</a>'})
    report = run(job(changed))
    assert report['nextIndexPage'] == 1
    assert changed.calls == [INDEX, INDEX]
    assert report['pendingReleases'] == 0
    assert '2026-08' not in report['missingMonths']['cn.cpi.core_yoy']


def test_empty_directory_keeps_missing_history_and_status_is_read_only(setup):
    factory, cursors, job = setup
    transport = Transport({INDEX: page(links=False)})
    current = job(transport)
    assert current.status()['status'] == 'not_started'
    report = run(current)
    assert report['discoveryComplete'] is True
    assert report['status'] == 'partial'
    assert len(report['missingMonths']['cn.cpi.core_yoy']) == 12
    assert len(report['missingMonths']['cn.industry.value_added_yoy']) == 10
    before = cursors.load(backfill.CURSOR_KEY)
    assert current.status()['missingMonths'] == report['missingMonths']
    assert transport.calls == [INDEX]
    assert cursors.load(backfill.CURSOR_KEY) == before


@pytest.mark.parametrize('change', [{'history_years': 2}, {'end': date(2026, 7, 31)}])
def test_changed_campaign_parameters_are_rejected_without_requests(setup, change):
    factory, cursors, job = setup
    run(job(Transport({INDEX: page(links=False)})))
    transport = Transport({})
    with pytest.raises(ValueError, match='NBS_HISTORY_CONFIG_MISMATCH'):
        run(job(transport), **change)
    assert not transport.calls


@pytest.mark.parametrize('legacy_hash', [
    '85c55cd9da189b751cafd3736d47095e920c0f8c6a7bac9db3ffa177c4562a31',
    '7c4787cfb46c09b4491c600d28ae27696259c6604e9741626b1367d3500f3fde',
])
def test_newline_legacy_checkpoint_resumes_without_rewriting_identity_or_done_entries(setup, legacy_hash):
    factory, cursors, job = setup
    run(job(Transport({INDEX: page(count=2), URL: BODY})))
    before = cursors.load(backfill.CURSOR_KEY)
    before['catalog_hash'] = legacy_hash
    cursors.save(backfill.CURSOR_KEY, before)
    resumed = Transport({INDEX: page(count=2), INDEX + 'index_1.html': page(1, 2, False)})
    report = run(job(resumed), as_of=AS_OF.replace(day=5))
    after = cursors.load(backfill.CURSOR_KEY)
    assert report['rowCount'] == 0 and report['pendingReleases'] == 0
    assert report['discoveryComplete'] is True
    assert after['catalog_hash'] == legacy_hash
    assert after['entries'] == before['entries']
    assert after['as_of'] == before['as_of']
    assert resumed.calls == [INDEX, INDEX + 'index_1.html']
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 1


@pytest.mark.parametrize('change', [{'catalog_hash': 'b' * 64}, {'catalog_version': 'unknown-v7'}])
def test_unverified_checkpoint_identity_is_rejected_before_requests_or_writes(setup, change):
    factory, cursors, job = setup
    run(job(Transport({INDEX: page(count=2), URL: BODY})))
    before = {**cursors.load(backfill.CURSOR_KEY), **change}
    cursors.save(backfill.CURSOR_KEY, before)
    transport = Transport({})
    with pytest.raises(ValueError, match='NBS_HISTORY_CONFIG_MISMATCH'):
        run(job(transport))
    assert transport.calls == []
    assert cursors.load(backfill.CURSOR_KEY) == before
    with factory() as session:
        assert len(session.scalars(select(JobRun)).all()) == 1


def test_malformed_cursor_is_rejected_before_network(setup):
    factory, cursors, job = setup
    cursors.save(backfill.CURSOR_KEY, {'url': 'https://evil.test', 'schemaVersion': '1.0'})
    transport = Transport({})
    with pytest.raises(ValueError, match='NBS_HISTORY_CHECKPOINT_INVALID'):
        run(job(transport))
    assert transport.calls == []


def test_locked_run_has_no_network_or_cursor_side_effect(setup):
    factory, cursors, job = setup
    lock = PostgresAdvisoryJobLock(factory.kw["bind"])
    key = data_publication_lock_key()
    assert lock.try_acquire(key)
    try:
        transport = Transport({})
        assert run(job(transport))['status'] == 'locked'
        assert transport.calls == []
        assert cursors.load(backfill.CURSOR_KEY) is None
    finally:
        lock.release(key)


def test_add_manifest_after_discovery_complete_resumes_new_reports(setup):
    factory, cursors, job = setup
    run(job(Transport({INDEX: page(links=False)})))
    result = run(job(Transport({URL: BODY, INDEX: page(links=False)})), releases=(RELEASE,))
    assert result['requestCount'] == 2
    assert result['rowCount'] == 1
    assert result['discoveryComplete'] is True


def test_same_period_conflict_is_not_confirmed_or_inserted(setup):
    factory, cursors, job = setup
    second_url = URL.replace('1965263', '1965264')
    second = RELEASE.model_copy(update={'url': second_url})
    report = run(job(Transport({URL: BODY, second_url: BODY.replace('<td>1.0</td>', '<td>9.0</td>')})), releases=(RELEASE, second))
    assert report['pendingReleases'] == 1
    assert '2026-08' in report['missingMonths']['cn.cpi.core_yoy']
    with factory() as session:
        rows = session.scalars(select(MacroObservation)).all()
        assert len(rows) == 1 and rows[0].value == Decimal('1.0')


def test_each_report_in_one_batch_has_distinct_raw_audit(setup):
    factory, cursors, job = setup
    second_url = URL.replace('1965263', '1965264')
    second = RELEASE.model_copy(update={'url': second_url})
    run(job(Transport({URL: BODY, second_url: BODY})), releases=(RELEASE, second))
    with factory() as session:
        payloads = session.scalars(select(SourcePayload)).all()
        assert len(payloads) == 2
        assert {p.payload['request']['source']['sourceUrl'] for p in payloads} == {URL, second_url}


def test_changed_manifest_title_cannot_silently_reuse_done_entry(setup):
    factory, cursors, job = setup
    run(job(Transport({URL: BODY, INDEX: page(links=False)})), releases=(RELEASE,))
    conflict = RELEASE.model_copy(update={'title': TITLE.replace('0.8', '0.9')})
    transport = Transport({})
    with pytest.raises(ValueError, match='NBS_HISTORY_MANIFEST_CONFLICT'):
        run(job(transport), releases=(conflict,))
    assert transport.calls == []


def test_failed_queue_larger_than_batch_does_not_starve_later_release(setup):
    factory, cursors, job = setup
    failed_a = RELEASE.model_copy(update={'url': URL.replace('1965263', '1965260')})
    failed_b = RELEASE.model_copy(update={'url': URL.replace('1965263', '1965261')})
    pages = {failed_a.url: '', failed_b.url: '', URL: BODY, INDEX: page(links=False)}
    run(job(Transport(pages)), releases=(failed_a, failed_b, RELEASE))
    second = run(job(Transport(pages)), releases=(failed_a, failed_b, RELEASE))
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 1
    assert second['rowCount'] == 1


def test_exhausted_directory_is_rechecked_for_late_index_entries(setup):
    factory, cursors, job = setup
    run(job(Transport({INDEX: page(links=False)})))
    changed = Transport({INDEX: page(), URL: BODY})
    result = run(job(changed), request_limit=3)
    assert result['rowCount'] == 1
    assert '2026-08' not in result['missingMonths']['cn.cpi.core_yoy']


def test_shortened_cursor_window_is_invalid_not_completed(setup):
    factory, cursors, job = setup
    run(job(Transport({INDEX: page(links=False)})))
    state = cursors.load(backfill.CURSOR_KEY)
    state['start'] = '2026-08-01'
    cursors.save(backfill.CURSOR_KEY, state)
    transport = Transport({})
    with pytest.raises(ValueError, match='NBS_HISTORY_CHECKPOINT_INVALID'):
        run(job(transport))
    assert not transport.calls


def test_manifest_identity_change_to_outside_window_is_rejected(setup):
    factory, cursors, job = setup
    run(job(Transport({URL: BODY, INDEX: page(links=False)})), releases=(RELEASE,))
    changed = RELEASE.model_copy(update={'title': TITLE.replace('2026', '2000')})
    transport = Transport({})
    with pytest.raises(ValueError, match='NBS_HISTORY_MANIFEST_CONFLICT'):
        run(job(transport), releases=(changed,))
    assert not transport.calls


def test_core_commit_then_interruption_cannot_accept_changed_report_value(setup, monkeypatch):
    factory, cursors, job = setup
    original = cursors.save
    def interrupted(key, value):
        if any(item['status'] == 'done' for item in value.get('entries', [])):
            raise KeyboardInterrupt()
        original(key, value)
    monkeypatch.setattr(cursors, 'save', interrupted)
    with pytest.raises(KeyboardInterrupt):
        run(job(Transport({URL: BODY})), releases=(RELEASE,))
    monkeypatch.setattr(cursors, 'save', original)
    result = run(job(Transport({URL: BODY.replace('<td>1.0</td>', '<td>9.0</td>'), INDEX: page(links=False)})), releases=(RELEASE,))
    assert result['pendingReleases'] == 1
    assert result['failures'][0]['code'] == 'NBS_HISTORY_CORE_CONFLICT'
    with factory() as session:
        assert [row.value for row in session.scalars(select(MacroObservation)).all()] == [Decimal('1.0')]


@pytest.mark.parametrize('existing', ['future', 'invalidated'])
def test_unchanged_requires_visible_effective_core_identity(setup, existing):
    factory, cursors, job = setup
    released = datetime(2026, 10, 9, 1, 30, tzinfo=UTC) if existing == 'future' else datetime(2026, 9, 9, 1, 30, tzinfo=UTC)
    until = AS_OF if existing == 'invalidated' else None
    observation = ProviderObservation('nbs', '不包括食品和能源', date(2026, 8, 1), released, None, Decimal('1.0'), URL, until)
    catalog = MacroSeriesCatalog.load(DEFAULT_MACRO_CATALOG_FILE)
    MacroObservationRepository(factory).persist(catalog.require('cn.cpi.core_yoy'), ProviderFetchResult('nbs', 'success', (observation,), {}, 1), AS_OF)
    result = run(job(Transport({URL: BODY, INDEX: page(links=False)})), releases=(RELEASE,))
    assert result['pendingReleases'] == 1
    assert '2026-08' in result['missingMonths']['cn.cpi.core_yoy']


def test_failed_long_queue_still_advances_index_after_anchor_checks(setup):
    factory, cursors, job = setup
    failed = tuple(RELEASE.model_copy(update={'url': URL.replace('1965263', str(1965250 + i))}) for i in range(3))
    pages = {item.url: '' for item in failed}
    pages.update({INDEX: page(count=2, links=False), INDEX + 'index_1.html': page(1, 2, False)})
    reports = [run(job(Transport(pages)), releases=failed) for _ in range(5)]
    assert reports[-1]['discoveryComplete'] is True
    assert reports[-1]['pendingReleases'] == 3


def test_completed_discovery_survives_over_one_hundred_failed_attempts(setup):
    factory, cursors, job = setup
    run(job(Transport({INDEX: page(links=False)})))
    failed = RELEASE.model_copy(update={'url': URL.replace('1965263', '1965260')})
    pages = {failed.url: '', INDEX: page(links=False), URL: BODY}
    for _ in range(105):
        assert run(job(Transport(pages)), releases=(failed,))['status'] == 'partial'
    result = run(job(Transport(pages)), releases=(failed, RELEASE))
    # 失败轮转与锚点复核可能占本批，下一批仍能前进。
    if result['rowCount'] == 0:
        result = run(job(Transport(pages)), releases=(failed, RELEASE))
    assert result['rowCount'] == 1


def complete_window(setup):
    factory, cursors, job = setup
    periods = [(2025, 9), (2025, 10), (2025, 11), (2025, 12),
               (2026, 1), (2026, 2), (2026, 3), (2026, 4), (2026, 5), (2026, 6), (2026, 7), (2026, 8)]
    releases = []
    pages = {INDEX: page(links=False)}
    for number, (year, month) in enumerate(periods):
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        for series_id in ('cn.cpi.core_yoy', 'cn.industry.value_added_yoy'):
            if series_id == 'cn.industry.value_added_yoy' and month in (1, 2):
                continue
            cpi = series_id == 'cn.cpi.core_yoy'
            title = f'{year}年{month}月份居民消费价格同比上涨0.8%' if cpi else f'{year}年{month}月份规模以上工业增加值增长5.2%'
            url = INDEX + f'{next_year}{next_month:02d}/t{next_year}{next_month:02d}10_{number + (10000 if cpi else 20000)}.html'
            releases.append(NbsReleaseLocator(series_id=series_id, url=url, title=title))
            text = (f'<p>{year}年{month}月份，全国居民消费价格同比上涨0.8%。</p><table><tr><td></td><td>环比涨跌幅（%）</td><td>同比涨跌幅（%）</td></tr><tr><td>不包括食品和能源</td><td>0.1</td><td>1.0</td></tr></table>' if cpi else f'<p>{month}月份，规模以上工业增加值同比实际增长5.2%。</p>')
            pages[url] = f'<meta name="ArticleTitle" content="{title}"><meta name="PubDate" content="{next_year}/{next_month:02d}/10 09:30">{text}'
    report = run(job(Transport(pages)), request_limit=100, releases=tuple(releases))
    return factory, cursors, job, pages, tuple(releases), report


def test_completed_requires_every_expected_month_in_real_core(setup):
    factory, cursors, job, pages, releases, report = complete_window(setup)
    assert report['status'] == 'completed'
    assert report['missingMonths'] == {'cn.cpi.core_yoy': [], 'cn.industry.value_added_yoy': []}
    assert report['rowCount'] == 22
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 22


def test_failed_anchor_cannot_report_this_batch_completed(setup):
    factory, cursors, job, pages, releases, initial = complete_window(setup)
    before = cursors.load(backfill.CURSOR_KEY)
    failed = run(job(Transport({INDEX: ProviderTransientError('HTTP_REQUEST_FAILED', request_count=1)})))
    assert failed['status'] == 'partial'
    assert failed['requestCount'] == 1 and failed['rowCount'] == 0
    assert failed['failureContexts'][0]['errorCode'] == 'HTTP_REQUEST_FAILED'
    assert failed['missingMonths'] == {'cn.cpi.core_yoy': [], 'cn.industry.value_added_yoy': []}
    assert cursors.load(backfill.CURSOR_KEY) == before
    with factory() as session:
        assert session.get(JobRun, failed['jobRunId']).status == 'partial'
        assert len(session.scalars(select(MacroObservation)).all()) == 22
    retried = run(job(Transport({INDEX: page(links=False)})))
    assert retried['status'] == 'completed'
    assert retried['requestCount'] == 1 and retried['rowCount'] == 0


def test_budget_exhausted_before_anchor_cannot_report_this_batch_completed(setup):
    factory, cursors, job, pages, releases, initial = complete_window(setup)
    duplicates = tuple(releases[0].model_copy(update={'url': INDEX + f'202510/t20251010_{number}.html'})
                       for number in (99990, 99991))
    bodies = {item.url: pages[releases[0].url] for item in duplicates}
    report = run(job(Transport(bodies)), releases=duplicates)
    assert report['status'] == 'partial'
    assert report['requestCount'] == 2 and report['rowCount'] == 0
    assert report['pendingReleases'] == 0 and report['discoveryComplete'] is True
    assert report['missingMonths'] == {'cn.cpi.core_yoy': [], 'cn.industry.value_added_yoy': []}
    retried = run(job(Transport({INDEX: page(links=False)})))
    assert retried['status'] == 'completed'
    with factory() as session:
        assert len(session.scalars(select(MacroObservation)).all()) == 22


@pytest.mark.parametrize('bad_anchor', [
    ProviderTransientError('HTTP_CLIENT_ERROR', request_count=1),
    page(links=False),  # 旧页返回缩短后的首页分页，不能接受为原锚点。
])
def test_missing_old_anchor_recovers_from_valid_official_home_within_budget(setup, bad_anchor):
    factory, cursors, job = setup
    original_pages = {INDEX: page(count=3, links=False), INDEX + 'index_1.html': page(1, 3, False),
                      INDEX + 'index_2.html': page(2, 3, False)}
    run(job(Transport(original_pages)))
    run(job(Transport(original_pages)))
    transport = Transport({INDEX + 'index_2.html': bad_anchor,
                           INDEX: page(), URL: BODY})
    first = run(job(transport))
    assert transport.calls == [INDEX + 'index_2.html', INDEX]
    assert first['requestCount'] == 2
    assert first['pendingReleases'] == 1
    assert first['nextIndexPage'] == 1
    second = run(job(Transport({URL: BODY, INDEX: page()})))
    assert second['rowCount'] == 1 and second['pendingReleases'] == 0


def test_home_anchor_failure_is_not_retried_in_same_batch(setup):
    factory, cursors, job = setup
    run(job(Transport({INDEX: page(links=False)})))
    before = cursors.load(backfill.CURSOR_KEY)
    failed = ProviderTransientError('HTTP_REQUEST_FAILED', request_count=1)
    transport = Transport({INDEX: failed})
    result = run(job(transport))
    # 上页本身是首页，不能同批隐式重试首页。
    assert result['status'] == 'partial' and result['requestCount'] == 1
    assert transport.calls == [INDEX]
    assert cursors.load(backfill.CURSOR_KEY) == before


@pytest.mark.parametrize('changed_title', ['outside_window', 'unrecognized'])
def test_directory_identity_change_outside_window_does_not_accept_progress(setup, changed_title):
    factory, cursors, job, pages, releases, initial = complete_window(setup)
    known = releases[0]
    before = cursors.load(backfill.CURSOR_KEY)
    title = known.title.replace('2025', '2000') if changed_title == 'outside_window' else 'unrecognized report'
    altered = page(links=False) + f'<a href="{known.url}" title="{title}">changed</a>'
    transport = Transport({INDEX: altered})
    result = run(job(transport))
    assert result['status'] == 'partial'
    assert result['failureContexts'][0]['errorCode'] == 'NBS_HISTORY_MANIFEST_CONFLICT'
    assert transport.calls == [INDEX]
    assert cursors.load(backfill.CURSOR_KEY) == before


@pytest.mark.parametrize('conflict', ['value', 'released_at'])
def test_conflicting_done_evidence_is_invalid_before_network(setup, conflict):
    factory, cursors, job, pages, releases, initial = complete_window(setup)
    state = cursors.load(backfill.CURSOR_KEY)
    contradictory = dict(state['entries'][0])
    contradictory['release'] = {**contradictory['release'], 'url': INDEX + '202510/t20251010_99999.html'}
    contradictory[conflict] = '9.0' if conflict == 'value' else '2025-10-11T01:30:00+00:00'
    state['entries'].append(contradictory)
    cursors.save(backfill.CURSOR_KEY, state)
    transport = Transport({INDEX: page(links=False)})
    with pytest.raises(ValueError, match='NBS_HISTORY_CHECKPOINT_INVALID'):
        run(job(transport))
    assert transport.calls == []


@pytest.mark.parametrize('change', ['missing', 'conflicting', 'invalidated'])
def test_resumed_done_requires_current_core_identity_and_visibility(setup, change):
    factory, cursors, job, pages, releases, initial = complete_window(setup)
    known = releases[0]
    with factory.begin() as session:
        observation = session.scalar(select(MacroObservation).where(
            MacroObservation.series_id == known.series_id, MacroObservation.observed_at == known.observed_at))
        if change == 'missing':
            session.delete(observation)
        elif change == 'conflicting':
            observation.value = Decimal('9.0')
        else:
            observation.valid_until = AS_OF
    # 只读 status 保留历史确认语义；执行续跑必须重新核对实际 core。
    assert job(Transport({})).status()['status'] == 'completed'
    transport = Transport({INDEX: page(links=False), known.url: ''})
    result = run(job(transport))
    assert result['status'] == 'partial'
    assert result['pendingReleases'] == 1
    assert '2025-09' in result['missingMonths']['cn.cpi.core_yoy']
    assert known.url in transport.calls
    assert result['rowCount'] == 0


def test_failed_old_anchor_and_failed_home_leave_checkpoint_unchanged(setup):
    factory, cursors, job = setup
    original_pages = {INDEX: page(count=3, links=False), INDEX + 'index_1.html': page(1, 3, False),
                      INDEX + 'index_2.html': page(2, 3, False)}
    run(job(Transport(original_pages)))
    run(job(Transport(original_pages)))
    before = cursors.load(backfill.CURSOR_KEY)
    failure = ProviderTransientError('HTTP_REQUEST_FAILED', request_count=1)
    transport = Transport({INDEX + 'index_2.html': failure, INDEX: failure})
    result = run(job(transport))
    assert result['status'] == 'partial'
    assert result['requestCount'] == 2 and len(result['failureContexts']) == 2
    assert transport.calls == [INDEX + 'index_2.html', INDEX]
    assert cursors.load(backfill.CURSOR_KEY) == before


def test_consistent_duplicate_done_with_equivalent_decimal_remains_valid(setup):
    factory, cursors, job, pages, releases, initial = complete_window(setup)
    state = cursors.load(backfill.CURSOR_KEY)
    consistent = dict(state['entries'][0])
    consistent['release'] = {**consistent['release'], 'url': INDEX + '202510/t20251010_99999.html'}
    consistent['value'] = '1.00'
    state['entries'].append(consistent)
    cursors.save(backfill.CURSOR_KEY, state)
    result = run(job(Transport({INDEX: page(links=False)})))
    assert result['status'] == 'completed' and result['rowCount'] == 0
