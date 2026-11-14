"""实际派息查询覆盖：原件、空响应及北交所旧代码。"""
import base64
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import pytest

from sqlalchemy import select

from app.connectors.tushare.contracts import TushareResponse
from app.db.models import JobCursor, PublishedSnapshot, Security, SourcePayload
from backend.tests.test_dividend_evidence_capture import source
from backend.tests.test_portfolio_routine import NOW, refresh
from backend.tests.test_dividend_ledger import saved

CURRENT = '920047.BJ'
OLD = '430047.BJ'


class Client:
    def __init__(self, *, empty=False, fail=None, mapping_size=1, mapping_more=False):
        self.calls = []
        self.empty = empty
        self.fail = fail
        self.mapping_size = mapping_size
        self.mapping_more = mapping_more

    def set_before_request(self, callback):
        self.before = callback

    def set_response_observer(self, callback):
        self.observer = callback

    def call(self, api, params, fields):
        self.before()
        self.calls.append((api, deepcopy(params)))
        if self.fail is not None and (api == self.fail or params.get('ts_code') == self.fail):
            raise TimeoutError('private-token')
        if api == 'bse_mapping':
            rows = [dict(o_code=OLD, n_code=CURRENT)] * self.mapping_size
        elif self.empty:
            rows = []
        else:
            row = dict.fromkeys(fields)
            row.update(ts_code=params['ts_code'], end_date='20251231', ann_date='20260501',
                imp_ann_date='20260531', div_proc='实施', record_date='20260601',
                ex_date='20260602', pay_date='20260603', cash_div_tax='0.40',
                stk_div=0, stk_bo_rate=0, stk_co_rate=0)
            rows = [row]
        data = dict(fields=list(fields), items=[[row[f] for f in fields] for row in rows])
        if api == 'bse_mapping' and self.mapping_more:
            data['has_more'] = True
        body = json.dumps(dict(code=0, data=data)).encode()
        self.observer(dict(statusCode=200, credentialsRedacted=False,
            bodyBase64=base64.b64encode(body).decode(), byteLength=len(body), bodySha256=sha256(body).hexdigest()))
        return TushareResponse.model_validate(data)


def factory(*, bse=False):
    f = source()
    with f.begin() as s:
        old = s.get(PublishedSnapshot, 'screener')
        symbol = CURRENT if bse else old.payload['stocks'][0]['symbol']
        if bse:
            old.payload = old.payload | dict(stocks=[dict(symbol=symbol)])
            s.add(Security(ts_code=symbol, symbol='920047', name='测试证券', list_date=date(2020, 11, 24)))
        else:
            s.get(Security, symbol).list_date = date(2001, 8, 27)
    return f, symbol


def coverage(f, symbol):
    from app.portfolio.api.dividends import dividend_query_coverage
    with f() as s:
        return dividend_query_coverage(s, [symbol])[symbol]


def test_successful_empty_response_has_explicit_query_and_listing_coverage():
    f, symbol = factory()
    assert refresh(f, Client(empty=True))['status'] == 'completed'
    item = coverage(f, symbol)
    assert item['querySource'].startswith('raw.source_payload:')
    assert item['sourceIds'] == [item['querySource']]
    assert item['coverageStart'] == '2001-08-27'
    assert item['coverageEnd'] == '2026-08-24'
    assert item['issues'] == []


def test_failed_response_never_becomes_empty_query_coverage():
    f, symbol = factory()
    assert refresh(f, Client(fail=symbol))['status'] == 'blocked'
    item = coverage(f, symbol)
    assert item['querySource'] is None and item['issues']
    assert item['coverageEnd'] is None


def test_mapping_and_old_code_share_budget_resume_and_merge_ledger():
    from app.portfolio.api.dividends import verified_dividend_rows, resolve_distribution_evidence
    from app.portfolio.marts.portfolio_ledger import load_portfolio_dividend_ledger
    f, symbol = factory(bse=True)
    client = Client()
    first = refresh(f, client, budget=1)
    assert first['status'] == 'pending' and first['requestCount'] == 1, first
    assert client.calls == [('bse_mapping', {})]
    assert coverage(f, symbol)['issues']
    second = refresh(f, client, budget=1)
    assert second['status'] == 'pending' and second['requestCount'] == 1
    third = refresh(f, client, budget=1)
    assert third['status'] == 'completed' and third['requestCount'] == 1
    assert {p['ts_code'] for api, p in client.calls if api == 'dividend'} == {CURRENT, OLD}
    assert refresh(f, client)['requestCount'] == 0
    with f() as s:
        primary, rows = verified_dividend_rows(s, [symbol])[symbol]
        assert primary and {r['ts_code'] for r in rows} == {symbol}
        assert {r['_originalTsCode'] for r in rows} == {CURRENT, OLD}
        assert len(resolve_distribution_evidence(rows, date(2026, 8, 24))['events']) == 1
    item = coverage(f, symbol)
    assert item['issues'] == [] and len(item['sourceIds']) == 3
    ledger = load_portfolio_dividend_ledger(f, (symbol,), date(2026, 8, 24), {symbol:'2026-01-01'})
    events = ledger['assets'][symbol]['events']
    assert len(events) == 1 and events[0]['cashPerShare'] == .4
    assert len(events[0]['sourceIds']) == 2
    assert {p['sourceId'] for p in events[0].get('sourceOriginals', [])} == set(events[0]['sourceIds'])


@pytest.mark.parametrize('alias', [False, True])
def test_ledger_event_metadata_belongs_to_actual_alias_or_historical_original(alias):
    from app.portfolio.marts.portfolio_ledger import load_portfolio_dividend_ledger, validate_dividend_ledger
    class OldOnlyClient(Client):
        def call(self, api, params, fields):
            self.empty = api == 'dividend' and params['ts_code'] == CURRENT
            return super().call(api, params, fields)
    f, symbol = factory(bse=alias)
    assert refresh(f, OldOnlyClient() if alias else Client())['status'] == 'completed'
    if not alias:
        assert refresh(f, Client(empty=True), now=NOW + timedelta(days=8))['status'] == 'completed'
    with f.begin() as s:
        raws = s.scalars(select(SourcePayload).where(SourcePayload.api_name == 'dividend').order_by(SourcePayload.id)).all()
        factual = next(raw for raw in raws if raw.payload['items'])
        primary = next(raw for raw in raws if not raw.payload['items'])
        factual.captured_at = datetime(2026, 8, 1, 1, tzinfo=timezone.utc)
        primary.captured_at = datetime(2026, 8, 2, 2, tzinfo=timezone.utc)
        source_id, expected_hash = f'raw.source_payload:{factual.id}:row:1', factual.content_hash
        assert primary.content_hash != expected_hash
    ledger = load_portfolio_dividend_ledger(f, (symbol,), date(2026, 8, 24), {symbol: '2026-01-01'})
    item = ledger['assets'][symbol]['events'][0]
    assert item['sourceIds'] == [source_id]
    assert item['sourceHash'] == expected_hash
    assert item['capturedAt'] == '2026-08-01T01:00:00+00:00'
    assert item['sourceOriginals'] == [dict(sourceId=source_id, sourceHash=expected_hash, capturedAt=item['capturedAt'])]
    assert validate_dividend_ledger(ledger, (symbol,), date(2026, 8, 24)) == ()
    for version in ledger['assets'][symbol]['historyVersions']:
        if version['events']:
            assert version['events'][0]['sourceHash'] == expected_hash


@pytest.mark.parametrize('change', ['unbound', 'hash', 'capture', 'empty'])
def test_ledger_gate_rejects_unbound_original_metadata(saved, change):
    from backend.tests.test_dividend_ledger import build, SYMBOL, DAY
    from app.portfolio.marts.portfolio_ledger import validate_dividend_ledger
    ledger = build(saved)
    item = ledger['assets'][SYMBOL]['events'][0]
    if change == 'unbound': item['sourceOriginals'][0]['sourceId'] = 'raw.source_payload:999:row:1'
    if change == 'hash': item['sourceOriginals'][0]['sourceHash'] = 'wrong'
    if change == 'capture': item['sourceOriginals'][0]['capturedAt'] = 'wrong'
    if change == 'empty': item['sourceOriginals'] = []
    assert validate_dividend_ledger(ledger, (SYMBOL,), DAY)


def test_alias_failure_keeps_successful_raw_and_does_not_grant_zero():
    f, symbol = factory(bse=True)
    client = Client(empty=True, fail=OLD)
    result = refresh(f, client)
    assert result['status'] == 'blocked'
    assert result['requestCount'] <= 3
    assert coverage(f, symbol)['issues']
    before = list(client.calls)
    result = refresh(f, client, now=NOW + timedelta(days=8))
    assert result['requestCount'] == 0 and client.calls == before
    with f() as s:
        assert s.scalars(select(SourcePayload)).all()


def test_mapping_failure_is_visible_and_never_queries_aliases_blindly():
    f, symbol = factory(bse=True)
    client = Client(fail='bse_mapping')
    result = refresh(f, client)
    assert result['status'] == 'blocked' and result['requestCount'] == 1
    assert client.calls == [('bse_mapping', {})]
    assert coverage(f, symbol)['issues']


def test_tampered_mapping_original_cannot_supply_alias_identity():
    f, symbol = factory(bse=True)
    result = refresh(f, Client())
    assert result['status'] == 'completed', result
    with f.begin() as s:
        raw = s.scalar(select(SourcePayload).where(SourcePayload.api_name == 'bse_mapping'))
        raw.payload = raw.payload | {'items': []}
    item = coverage(f, symbol)
    assert item['issues'] and item['querySource'] is None


def test_cli_preview_discovers_mapping_after_legacy_primary_is_complete():
    from app.portfolio.dividend_capture import capture_dividend_evidence, run_dividend_capture
    from backend.tests.test_dividend_evidence_capture import config
    from types import SimpleNamespace
    f, symbol = factory(bse=True)
    client = Client(empty=True)
    assert capture_dividend_evidence(f, client, execute=True, config=config())['status'] == 'completed'
    preview = run_dividend_capture(SimpleNamespace(), f)
    assert preview['status'] == 'planned' and preview['pendingCount'] > 0, preview
    result = refresh(f, client)
    assert result['status'] == 'completed'
    assert [params['ts_code'] for api, params in client.calls if api == 'dividend'] == [symbol, OLD]
    assert coverage(f, symbol)['issues'] == []


def test_missing_bse_mapping_does_not_remove_unrelated_security_coverage():
    from app.portfolio.api.dividends import dividend_query_coverage
    f, symbol = factory()
    assert refresh(f, Client(empty=True))['status'] == 'completed'
    with f.begin() as s:
        s.add(Security(ts_code=CURRENT, symbol='920047', name='测试证券', list_date=date(2020, 11, 24)))
    with f() as s:
        values = dividend_query_coverage(s, [symbol, CURRENT])
    assert values[symbol]['issues'] == [] and values[symbol]['querySource']
    assert values[CURRENT]['issues'] and values[CURRENT]['querySource'] is None


def test_failed_mapping_revision_invalidates_old_query_coverage():
    f, symbol = factory(bse=True)
    assert refresh(f, Client(empty=True))['status'] == 'completed'
    assert coverage(f, symbol)['issues'] == []
    result = refresh(f, Client(fail='bse_mapping'), now=NOW + timedelta(days=8))
    assert result['status'] == 'blocked' and result['requestCount'] == 1
    item = coverage(f, symbol)
    assert item['issues'] and item['querySource'] is None


def test_mapping_at_supplier_1000_row_cap_never_grants_complete_coverage():
    f, symbol = factory(bse=True)
    result = refresh(f, Client(mapping_size=1000))
    assert result['status'] == 'blocked' and result['requestCount'] == 1
    assert coverage(f, symbol)['issues']


def test_fiscal_loader_isolates_missing_mapping_from_unrelated_security():
    from app.stocks.derived.metric_dividends import load_dividend_years
    f, symbol = factory()
    assert refresh(f, Client(empty=True))['status'] == 'completed'
    with f.begin() as s:
        s.add(Security(ts_code=CURRENT, symbol='920047', name='测试证券', list_date=date(2020, 11, 24)))
    with f() as s:
        years = load_dividend_years(s, [symbol, CURRENT], date(2026, 8, 24))
    assert years[symbol]['2025']['covered']
    assert not years[CURRENT]['2025']['covered'] and years[CURRENT]['2025']['issues']


def test_invalid_security_original_is_isolated_in_coverage_result():
    from app.portfolio.api.dividends import dividend_query_coverage, DIVIDEND_FIELDS
    from app.connectors.tushare.contracts import request_hash
    other = '600002.SH'
    f, symbol = factory()
    with f.begin() as s:
        base = s.get(PublishedSnapshot, 'screener')
        base.payload = base.payload | dict(stocks=[dict(symbol=symbol), dict(symbol=other)])
        s.add(Security(ts_code=other, symbol='600002', name='测试证券', list_date=date(2000, 1, 1)))
    assert refresh(f, Client(empty=True))['status'] == 'completed'
    with f.begin() as s:
        raw = s.scalar(select(SourcePayload).where(SourcePayload.api_name == 'dividend',
            SourcePayload.request_hash == request_hash('dividend', {'ts_code':other}, DIVIDEND_FIELDS)))
        raw.content_hash = 'tampered'
    with f() as s:
        values = dividend_query_coverage(s, [symbol, other])
    assert values[symbol]['issues'] == [] and values[symbol]['querySource']
    assert values[other]['issues'] and values[other]['querySource'] is None


def test_missing_listing_date_isolated_in_mixed_portfolio_publication():
    from app.db.models import DailyPrice, AdjustmentFactor
    from app.portfolio.marts.portfolio_runtime import rebuild_portfolio
    from app.portfolio.dividend_yield_inputs import valid_yield_inputs
    f, symbol = factory()
    other = '600002.SH'
    with f.begin() as s:
        base = s.get(PublishedSnapshot, 'screener')
        base.payload = base.payload | dict(stocks=[*base.payload['stocks'], dict(symbol=other, price=10)])
        s.add(Security(ts_code=other, symbol='600002', name='缺上市日期', list_date=None))
        s.add(DailyPrice(ts_code=other, trade_date=date(2026, 8, 24), close=10))
        for code in (symbol, other):
            s.add(AdjustmentFactor(ts_code=code, trade_date=date(2026, 8, 24), adj_factor=1))
    assert refresh(f, Client(empty=True))['status'] == 'completed'
    assert coverage(f, other)['querySource'] is None
    report = rebuild_portfolio(f, dry_run=False)
    assert report['status'] == 'completed', json.dumps(report, ensure_ascii=True)
    with f() as s:
        payload = s.get(PublishedSnapshot, 'portfolio').payload
    ledger, inputs = payload['dividendLedger'], payload['dividendYieldInputs']
    assert valid_yield_inputs(inputs, ledger, '2026-08-24')
    assert ledger['assets'][other]['querySource'] is None
    assert any('上市日期' in gap['reason'] for gap in ledger['assets'][other]['gaps'])
    assert inputs[other]['querySource'] is None and 'fiscalYears' not in inputs[other]
    assert inputs[symbol]['querySource'] == ledger['assets'][symbol]['querySource']
    assert inputs[symbol]['fiscalYears']['2025']['covered']
    assert inputs[symbol]['fiscalYears']['2025']['zeroConfirmed']


def test_truncated_mapping_original_retains_physical_request_count_and_blocks():
    f, symbol = factory(bse=True)
    client = Client(mapping_more=True)
    result = refresh(f, client)
    assert result['status'] == 'blocked' and result['requestCount'] == 1, result
    assert coverage(f, symbol)['issues']
    assert refresh(f, client)['requestCount'] == 0 and len(client.calls) == 1


def test_ledger_never_accepts_caller_rows_in_place_of_verified_originals():
    from app.portfolio.evidence import routine_ledger_evidence
    from app.portfolio.api.dividends import build_dividend_ledger
    f, symbol = factory()
    assert refresh(f, Client())['status'] == 'completed'
    with f() as s:
        base = s.get(PublishedSnapshot, 'screener')
        proof = routine_ledger_evidence(s, base, (symbol,), date(2026, 8, 24))
        proof['rows'] = {symbol: (proof['units'][symbol]['rawId'], [])}
        ledger = build_dividend_ledger(s, base, (symbol,), date(2026, 8, 24),
            {symbol: '2026-01-01'}, evidence=proof)
    assert len(ledger['assets'][symbol]['events']) == 1
    assert ledger['assets'][symbol]['events'][0]['cashPerShare'] == .4


def test_incomplete_alias_query_cannot_supply_ledger_zero_proof():
    from app.portfolio.dividend_capture import capture_dividend_evidence
    from app.portfolio.marts.portfolio_ledger import load_portfolio_dividend_ledger
    from backend.tests.test_dividend_evidence_capture import config
    f, symbol = factory(bse=True)
    client = Client(empty=True)
    assert capture_dividend_evidence(f, client, execute=True, config=config())['status'] == 'completed'
    assert refresh(f, Client(empty=True, fail=OLD))['status'] == 'blocked'
    ledger = load_portfolio_dividend_ledger(f, (symbol,), date(2026, 8, 24), {symbol:'2026-01-01'})
    asset = ledger['assets'][symbol]
    assert not asset.get('querySource') and asset['gaps']


def test_ledger_coverage_never_advances_with_manifest_metadata_alone():
    from app.portfolio.marts.portfolio_ledger import load_portfolio_dividend_ledger
    f, symbol = factory()
    client = Client(empty=True)
    assert refresh(f, client)['status'] == 'completed'
    with f.begin() as s:
        base = s.get(PublishedSnapshot, 'screener')
        base.as_of += timedelta(days=1)
        base.version = 'next-day'
        from app.portfolio.evidence import _get, _save, _seal
        manifest = _get(s, 'manifest').cursor_value
        _save(s, 'manifest', _seal({k:v for k,v in manifest.items() if k != 'checksum'} |
            dict(baseVersion=base.version, asOf='2026-08-25')))
    ledger = load_portfolio_dividend_ledger(f, (symbol,), date(2026, 8, 25), {symbol:'2026-01-01'})
    assert ledger['assets'][symbol]['coverageEnd'] == '2026-08-24'


@pytest.mark.parametrize('change', [{'cash_div_tax':'0'}, {'div_proc':'取消'}])
def test_ledger_protects_paid_cash_from_later_zero_or_cancellation(saved, change):
    from backend.tests.test_dividend_ledger import row, build, SYMBOL
    saved[5]([row(), row(ann_date='20260601', imp_ann_date='20260601', **change)])
    asset = build(saved)['assets'][SYMBOL]
    assert any(gap['reason'] == 'paid_revision_conflict' for gap in asset['gaps'])
    assert asset['events'] == []


def test_ledger_keeps_first_cash_publication_when_later_version_adds_capital(saved):
    from backend.tests.test_dividend_ledger import row, build, SYMBOL
    original = row(end_date='20241231', ann_date='20250415', imp_ann_date='20250415',
        cash_div_tax='0.4', record_date='20250519', ex_date='20250520', pay_date='20250520',
        stk_div=None, stk_bo_rate=None, stk_co_rate=None, div_listdate=None)
    updated = original | dict(ann_date='20250601', imp_ann_date='20250601',
        stk_div='0.2', stk_bo_rate='0.2', stk_co_rate='0', div_listdate='20250521')
    saved[5]([original, updated])
    asset = build(saved, price_starts={SYMBOL:'2025-01-01'})['assets'][SYMBOL]
    item = asset['events'][0]
    versions = asset.get('historyVersions', [])
    assert versions
    early = max((v for v in versions if v['knownAt'] <= '2025-05-30'), key=lambda v: v['knownAt'])
    assert early['events'][0]['cashPerShare'] == .4
    assert early['events'][0]['sourceIds'] == [f'raw.source_payload:{saved[4].id}:row:1']
    assert early['events'][0]['stockRatio'] is None
    assert early['events'][0]['capitalStatus'] == 'missing'
    assert item['announcedAt'] == item['implementationDate'] == '2025-06-01'
    assert item['cashPerShare'] == .4 and item['stockRatio'] == .2


def test_ledger_gate_rejects_invalid_history_version_time(saved):
    from backend.tests.test_dividend_ledger import build, SYMBOL, DAY
    from app.portfolio.marts.portfolio_ledger import validate_dividend_ledger
    model = build(saved)
    asset = model['assets'][SYMBOL]
    asset['historyVersions'] = [dict(knownAt='2030-01-01', events=asset['events'], gaps=[], issues=[])]
    assert validate_dividend_ledger(model, (SYMBOL,), DAY)


def test_ledger_gate_rejects_bad_history_event_date_without_raising(saved):
    from backend.tests.test_dividend_ledger import build, SYMBOL, DAY
    from app.portfolio.marts.portfolio_ledger import validate_dividend_ledger
    model = build(saved)
    asset = model['assets'][SYMBOL]
    version = next(v for v in asset['historyVersions'] if v['events'])
    version['events'][0]['announcedAt'] = 123
    assert validate_dividend_ledger(model, (SYMBOL,), DAY)


@pytest.mark.parametrize('bse', [False, True])
def test_new_base_day_refreshes_cash_queries_without_requerying_identity_metadata(bse):
    from app.stocks.derived.metric_dividends import load_dividend_years
    f, symbol = factory(bse=bse)
    client = Client(empty=True)
    assert refresh(f, client)['status'] == 'completed'
    mapping_count = sum(api == 'bse_mapping' for api, _ in client.calls)
    with f.begin() as s:
        base = s.get(PublishedSnapshot, 'screener')
        base.as_of += timedelta(days=1)
        base.version = 'next-day'
    result = refresh(f, client, now=NOW + timedelta(days=1))
    assert result['status'] == 'completed' and result['requestCount'] == (2 if bse else 1), result
    assert sum(api == 'bse_mapping' for api, _ in client.calls) == mapping_count
    with f() as s:
        years = load_dividend_years(s, [symbol], date(2026, 8, 25))
    assert years[symbol]['2025']['covered']
    assert coverage(f, symbol)['coverageEnd'] == '2026-08-25'
