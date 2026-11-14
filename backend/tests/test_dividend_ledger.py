"""只读账本：原件绑定、精度、时点、窗口和未知传播。"""
import base64
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import importlib
import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.connectors.tushare.contracts import request_hash
from app.db.models import IngestionBatch, JobCursor, PublishedSnapshot, SourcePayload, Security
from app.ingestion.repositories import content_hash
from app.portfolio.dividend_capture import JOB_NAME, DATASET
from app.portfolio.dividend_report import DIVIDEND_FIELDS

DAY = date(2026, 10, 30)
SYMBOL = '600001.SH'


def row(**changes):
    value = dict.fromkeys(DIVIDEND_FIELDS)
    value.update(ts_code=SYMBOL, end_date='20251231', ann_date='20260501',
        imp_ann_date='20260501', div_proc='实施', record_date='20260510',
        ex_date='20260511', pay_date='20260512', div_listdate='20260513',
        cash_div_tax='0.5', stk_div='0.2')
    return value | changes


def evidence(rows):
    return importlib.import_module('app.portfolio.dividend_contract').resolve_distribution_evidence(rows, DAY)


@pytest.fixture
def saved():
    engine = create_engine('sqlite://', execution_options={'schema_translate_map':{'raw':None, 'ops':None, 'mart':None, 'core':None}})
    for model in (IngestionBatch, SourcePayload, JobCursor, PublishedSnapshot, Security):
        model.__table__.create(engine)
    with Session(engine, expire_on_commit=False) as s:
        base = PublishedSnapshot(product='screener', version='base-one', as_of=datetime(2026,10,30,tzinfo=timezone.utc), payload={})
        frozen = dict(baseVersion='base-one', asOf='2026-10-30', symbols=[SYMBOL], apiName='dividend', fields=list(DIVIDEND_FIELDS))
        frozen['identity'] = content_hash(frozen)
        control = JobCursor(job_name=JOB_NAME, cursor_key='control', cursor_value=frozen)
        batch = IngestionBatch(id='batch-one', source='tushare', dataset=DATASET, status='completed', request_count=1, row_count=1, rejected_count=0)
        raw = SourcePayload(batch_id=batch.id, api_name='dividend', request_hash=request_hash('dividend', {'ts_code':SYMBOL}, DIVIDEND_FIELDS),
            content_hash='', payload={}, captured_at=datetime(2026,10,31,2,3,tzinfo=timezone.utc))
        s.add_all([base,control,batch,raw, Security(ts_code=SYMBOL, symbol='600001', name='测试证券', list_date=date(2000, 1, 1))]); s.flush()
        unit = JobCursor(job_name=JOB_NAME, cursor_key=f"{frozen['identity']}:unit:{SYMBOL}", cursor_value={})
        s.add(unit)

        def put(rows, body_text=None):
            data = {'fields':list(DIVIDEND_FIELDS), 'items':[[r[f] for f in DIVIDEND_FIELDS] for r in rows]}
            body = (body_text or json.dumps({'code':0, 'data':data})).encode()
            raw.payload = data | {'_http':dict(statusCode=200, bodyBase64=base64.b64encode(body).decode(), bodySha256=sha256(body).hexdigest(), byteLength=len(body), credentialsRedacted=False)}
            raw.content_hash = content_hash(raw.payload)
            batch.row_count = len(rows)
            unit.cursor_value = dict(status='completed', symbol=SYMBOL, batchId=batch.id, rawId=raw.id,
                requestHash=raw.request_hash, attemptObserved=True, responseContentHash=raw.content_hash, responseRowCount=len(rows))
            s.commit()
        put([row()])
        yield s, base, control, unit, raw, put
    engine.dispose()


def build(saved, **changes):
    s, base, *_ = saved
    args = dict(session=s, screener_base=base, symbols=(SYMBOL,), as_of=DAY, price_starts={SYMBOL:'2026-05-11'})
    args.update(changes)
    return importlib.import_module('app.portfolio.dividend_ledger').build_dividend_ledger(**args)


def test_evidence_keeps_capital_only_and_pending_cash():
    result = evidence([row(cash_div_tax=None, pay_date=None)])
    assert len(result['events']) == 1
    assert result['events'][0]['stk_div'] == Decimal('0.2')


def test_listdate_participates_in_conflicts_and_date_validation():
    assert not evidence([row(), row(div_listdate='20260514')])['events']
    assert not evidence([row(div_listdate='bad')])['events']
    assert not evidence([row(div_listdate='20260509')])['events']


def test_readonly_json_dto_keeps_window_before_registration_and_actual_capture(saved):
    s = saved[0]
    def no_writes(connection, cursor, statement, *args):
        assert statement.lstrip().upper().startswith('SELECT')
    event.listen(s.bind, 'before_cursor_execute', no_writes)
    result = build(saved)
    assert result['sourceStatus'] == 'observed' and result['historyComplete'] is False
    asset = result['assets'][SYMBOL]
    assert asset['status'] == 'observed' and asset['coverageStart'] == '2026-05-11'
    item = asset['events'][0]
    assert item['recordDate'] == '2026-05-10' and item['stockRatio'] == .2
    assert item['capturedAt'].startswith('2026-10-31T02:03:00')
    assert item['sourceIds'] == [f'raw.source_payload:{saved[4].id}:row:1']
    assert item['cashStatus'] == 'paid' and item['capitalStatus'] == 'reported-total'
    json.dumps(result, allow_nan=False)


def test_complete_empty_security_response_retains_query_evidence(saved):
    saved[5]([])
    asset = build(saved)['assets'][SYMBOL]
    assert asset['querySource'] == f'raw.source_payload:{saved[4].id}'
    assert (asset['events'], asset['gaps']) == ([], [])


@pytest.mark.parametrize('kind', ['date','scope','fields','identity','api'])
def test_frozen_identity_and_scope_fail_closed(saved, kind):
    control = saved[2]
    value = deepcopy(control.cursor_value)
    field, bad = {'parent':('baseVersion','other'), 'date':('asOf','2026-10-29'), 'scope':('symbols',[]),
        'fields':('fields',[]), 'identity':('identity','bad'), 'api':('apiName','other')}[kind]
    value[field] = bad; control.cursor_value = value; saved[0].commit()
    result = build(saved)
    assert result['sourceStatus'] in {'base_changed','invalid_evidence'}
    assert result['assets'][SYMBOL]['events'] == []


def test_same_day_same_scope_new_parent_keeps_original_capture_lineage(saved):
    saved[1].version = 'rebuilt-parent'; saved[0].commit()
    result = build(saved)
    assert result['sourceStatus'] == 'observed'
    assert result['sourceBaseVersion'] == 'base-one'


@pytest.mark.parametrize('kind', ['hash','length','body','normalized','unit','status'])
def test_http_bytes_and_normalized_binding_fail_closed(saved, kind):
    raw, unit = saved[4], saved[3]
    payload = deepcopy(raw.payload)
    if kind == 'hash': payload['_http']['bodySha256'] = 'bad'
    if kind == 'length': payload['_http']['byteLength'] += 1
    if kind == 'body': payload['_http']['bodyBase64'] = 'invalid!'
    if kind == 'normalized': payload['items'][0][DIVIDEND_FIELDS.index('cash_div_tax')] = '100'
    if kind == 'status': payload['_http']['statusCode'] = 500
    raw.payload = payload; raw.content_hash = content_hash(payload)
    unit.cursor_value = unit.cursor_value | {'responseContentHash':raw.content_hash}
    if kind == 'unit': unit.cursor_value = unit.cursor_value | {'batchId':'wrong'}
    saved[0].commit()
    assert build(saved)['sourceStatus'] == 'invalid_evidence'


def test_missing_capture_has_every_requested_asset_without_fixture(saved):
    saved[0].delete(saved[2]); saved[0].commit()
    result = build(saved, symbols=(SYMBOL,'000002.SZ'))
    assert result['sourceStatus'] == 'not_captured'
    assert set(result['assets']) == {SYMBOL,'000002.SZ'}
    assert all(a['status']=='missing' and not a['events'] for a in result['assets'].values())


def test_future_legs_are_pending_and_total_zero_is_direct_evidence(saved):
    saved[5]([row(pay_date='20261102', div_listdate='20261103', stk_div='0', stk_bo_rate=None, stk_co_rate=None)])
    item = build(saved)['assets'][SYMBOL]['events'][0]
    assert item['stockRatio'] == 0 and item['capitalStatus'] == 'reported-total'
    assert item['cashStatus'] == 'pending' and item['payDate'] is None and item['stockListedDate'] is None
    assert item['stockStatus'] == 'not_required'


def test_capital_conflict_and_rejected_event_gaps_but_not_proposals(saved):
    saved[5]([row(stk_div='0.2', stk_bo_rate='0.3'), row(end_date='20241231', div_proc='预案')])
    asset = build(saved)['assets'][SYMBOL]
    assert asset['events'][0]['capitalStatus'] == 'conflict' and asset['events'][0]['stockRatio'] is None
    saved[5]([row(),row(cash_div_tax='0.6'),row(end_date='20241231',div_proc='预案')])
    asset = build(saved)['assets'][SYMBOL]
    assert not asset['events'] and asset['gaps']
    assert all('stage_not_implemented' not in gap['reason'] for gap in asset['gaps'])


def test_decimal_body_survives_float_normalization_and_underflow_is_not_zero(saved):
    data = {'fields':list(DIVIDEND_FIELDS), 'items':[[row(cash_div_tax=0.1)[f] for f in DIVIDEND_FIELDS]]}
    body = json.dumps({'code':0,'data':data}).replace('0.1,', '0.10000000000000000000001,')
    saved[5]([row(cash_div_tax=0.1)], body)
    assert build(saved)['assets'][SYMBOL]['events'][0]['exactCash'] == '0.10000000000000000000001'
    saved[5]([row(cash_div_tax='1e-400',stk_div='1e-400')])
    asset = build(saved)['assets'][SYMBOL]
    assert asset['events'][0]['cashPerShare'] is None and asset['events'][0]['stockRatio'] is None
    assert 'exactCash' not in asset['events'][0] and 'exactStockRatio' not in asset['events'][0]
    assert asset['gaps']


def test_historical_events_outside_price_window_are_not_unrolled(saved):
    saved[5]([row(pay_date='20260510',ex_date='20260510',div_listdate='20260510')])
    assert build(saved)['assets'][SYMBOL]['events'] == []


def test_capital_only_survives_ledger_without_payment(saved):
    saved[5]([row(cash_div_tax=None,pay_date=None)])
    asset = build(saved)['assets'][SYMBOL]
    item = asset['events'][0]
    assert item['stockRatio'] == .2 and item['stockListedDate'] == '2026-05-13'
    assert item['cashStatus'] == 'missing' and item['cashPerShare'] is None
    assert 'exactCash' not in item
    assert any(gap['affects']=='cash' for gap in asset['gaps'])


def test_unlocated_cancel_carries_unknown_gap_and_proposals_do_not(saved):
    saved[5]([row(),row(div_proc='停止实施',record_date=None,ex_date=None,ann_date='20260601')])
    asset = build(saved)['assets'][SYMBOL]
    assert not asset['events']
    assert any('unlocated_cancellation' in gap['reason'] for gap in asset['gaps'])
    saved[5]([row(div_proc='预案')])
    asset = build(saved)['assets'][SYMBOL]
    assert not asset['events'] and not asset['gaps']


def test_stopped_proposal_without_implemented_fact_does_not_create_ledger_gap(saved):
    saved[5]([row(div_proc='停止实施', record_date=None, ex_date=None, pay_date=None,
                  div_listdate=None)])
    asset = build(saved)['assets'][SYMBOL]
    assert asset['querySource']
    assert asset['events'] == [] and asset['gaps'] == []


def test_stopped_proposal_does_not_reference_a_not_yet_public_implementation(saved):
    saved[5]([row(div_proc='停止实施', record_date=None, ex_date=None, pay_date=None, div_listdate=None),
              row(ann_date='20261101', imp_ann_date='20261101')])
    asset = build(saved)['assets'][SYMBOL]
    assert asset['events'] == [] and asset['gaps'] == []


def test_explicit_zero_cash_and_stock_need_no_qualification_or_payment_dates(saved):
    saved[5]([row(cash_div_tax='0', stk_div='0', record_date=None, ex_date=None,
                  pay_date=None, div_listdate=None)])
    asset = build(saved)['assets'][SYMBOL]
    assert asset['gaps'] == []
    assert asset['events'][0]['cashStatus'] == asset['events'][0]['stockStatus'] == 'not_required'


def test_cancellation_and_revision_share_original_contract_rules(saved):
    saved[5]([row(),row(ann_date='20260502',cash_div_tax='0.8')])
    assert build(saved)['assets'][SYMBOL]['events'][0]['cashPerShare'] == .8
    saved[5]([row(),row(ann_date='20260502',div_proc='取消实施')])
    asset = build(saved)['assets'][SYMBOL]
    assert not asset['events'] and not asset['gaps']


def test_exact_decimal_same_day_conflict_is_not_hidden_by_float_rounding(saved):
    values = [row(cash_div_tax=0.1),row(cash_div_tax=0.1)]
    data = {'fields':list(DIVIDEND_FIELDS), 'items':[[r[f] for f in DIVIDEND_FIELDS] for r in values]}
    body = json.dumps({'code':0,'data':data})
    body = body.replace('0.1,','0.10000000000000000000001,',1)
    saved[5](values,body)
    asset = build(saved)['assets'][SYMBOL]
    assert not asset['events'] and any('same_day_conflict' in gap['reason'] for gap in asset['gaps'])


@pytest.mark.parametrize('changes', [dict(stk_div=None,stk_bo_rate='0',stk_co_rate='0'),
    dict(stk_div='0.3',stk_bo_rate='0.1',stk_co_rate='0.1'),
    dict(stk_div='0',stk_bo_rate='0.1')])
def test_unknown_and_conflicting_capital_never_become_zero(saved,changes):
    saved[5]([row(**changes)])
    asset = build(saved)['assets'][SYMBOL]
    assert asset['events'][0]['stockRatio'] is None
    assert any(gap['affects']=='capital' for gap in asset['gaps'])


def test_bad_input_and_listed_date_conflict_propagate_gaps(saved):
    saved[5]([row(div_listdate='20260509')])
    asset = build(saved)['assets'][SYMBOL]
    assert not asset['events'] and asset['gaps']
    saved[5]([row(),row(div_listdate='20260514')])
    asset = build(saved)['assets'][SYMBOL]
    assert not asset['events'] and asset['gaps']


def test_utc_parent_day_and_unrelated_payload_scope(saved):
    from datetime import timedelta
    saved[1].as_of = datetime(2026,10,31,1,tzinfo=timezone(timedelta(hours=8)))
    saved[1].payload = {'stocks':[{'symbol':'000099.SZ'}]}
    assert build(saved)['sourceStatus'] == 'observed'


def test_future_announcement_does_not_erase_known_revision_or_add_gap(saved):
    saved[5]([row(),row(ann_date='20261101',cash_div_tax='8')])
    asset = build(saved)['assets'][SYMBOL]
    assert asset['events'][0]['cashPerShare'] == .5 and not asset['gaps']


def test_pending_raw_payment_remains_explainable_and_missing_stock_date_is_gap(saved):
    saved[5]([row(pay_date='20261102',div_listdate=None)])
    asset = build(saved)['assets'][SYMBOL]
    assert asset['events'][0]['cashStatus'] == 'pending'
    assert '2026-11-02' in asset['events'][0]['reason']
    assert any(gap['affects']=='capital' for gap in asset['gaps'])


@pytest.mark.parametrize(('changes','cash_status','stock_status'), [
    ({'cash_div_tax':'0','pay_date':None,'stk_div':'0','div_listdate':None},'not_required','not_required'),
    ({'div_listdate':'20261102'},'paid','pending'),
    ({'div_listdate':None},'paid','missing'),
    ({},'paid','credited'),
])
def test_leg_status_is_explicit_and_zero_cash_does_not_require_payment_date(saved,changes,cash_status,stock_status):
    saved[5]([row(**changes)])
    asset = build(saved)['assets'][SYMBOL]
    item = asset['events'][0]
    assert item['cashStatus'] == cash_status and item['stockStatus'] == stock_status
    if stock_status == 'pending':
        assert item['stockListedDate'] is None
        assert not any(gap['affects']=='capital' for gap in asset['gaps'])
    if cash_status == 'not_required':
        assert not asset['gaps']


def test_unanchored_events_have_unique_ids_and_pass_shared_dto_gate(saved):
    from app.portfolio.marts.portfolio_ledger import validate_dividend_ledger
    saved[5]([row(record_date=None,ex_date=None),row(record_date=None,ex_date=None,pay_date='20260514')])
    result = build(saved)
    assert validate_dividend_ledger(result, (SYMBOL,), DAY) == ()
    assert len({item['id'] for item in result['assets'][SYMBOL]['events']}) == 2


def test_unit_transport_pointer_cannot_bind_another_original(saved):
    saved[3].cursor_value = saved[3].cursor_value | {'transportRawId':saved[4].id + 1}
    saved[0].commit()
    assert build(saved)['sourceStatus'] == 'invalid_evidence'


@pytest.mark.parametrize('tamper', [None, 'items', 'fields', 'hash', 'unit'])
def test_response_metadata_does_not_replace_strict_fact_binding(saved, tamper):
    """合法has_more=false不属于规范事实块；仍不能掩盖任何错绑。"""
    values = [row()]
    data = {'fields':list(DIVIDEND_FIELDS),
            'items':[[values[0][field] for field in DIVIDEND_FIELDS]], 'has_more':False}
    saved[5](values, json.dumps({'code':0, 'data':data}))
    raw, unit = saved[4], saved[3]
    payload = deepcopy(raw.payload)
    if tamper == 'items':
        payload['items'][0][DIVIDEND_FIELDS.index('cash_div_tax')] = '100'
    elif tamper == 'fields':
        payload['fields'] = payload['fields'][:-1]
    elif tamper == 'hash':
        payload['_http']['bodySha256'] = 'bad'
    raw.payload = payload
    raw.content_hash = content_hash(payload)
    unit.cursor_value = unit.cursor_value | {'responseContentHash':raw.content_hash}
    if tamper == 'unit':
        unit.cursor_value = unit.cursor_value | {'batchId':'wrong'}
    saved[0].commit()
    result = build(saved)
    assert result['sourceStatus'] == ('observed' if tamper is None else 'invalid_evidence')


@pytest.mark.parametrize('count', [-1, 0, 1, 999])
def test_response_count_is_not_assumed_to_be_returned_row_count(saved, count):
    values = [row()]
    data = {'fields':list(DIVIDEND_FIELDS), 'items':[[values[0][f] for f in DIVIDEND_FIELDS]],
            'has_more':False, 'count':count}
    saved[5](values, json.dumps({'code':0, 'data':data}))
    result = build(saved)
    assert result['sourceStatus'] == 'observed'
    assert result['historyComplete'] is False


@pytest.mark.parametrize('has_more', [True, 0, 1, None, 'false'])
def test_response_pagination_cannot_silently_truncate_evidence(saved, has_more):
    values = [row()]
    data = {'fields':list(DIVIDEND_FIELDS), 'items':[[values[0][f] for f in DIVIDEND_FIELDS]],
            'has_more':has_more}
    saved[5](values, json.dumps({'code':0, 'data':data}))
    assert build(saved)['sourceStatus'] == 'invalid_evidence'


@pytest.mark.parametrize('listed,expected_status', [('20260513','missing'),('20261102','pending')])
def test_unknown_ratio_gap_starts_at_known_listing_not_registration(saved, listed, expected_status):
    saved[5]([row(stk_div=None, div_listdate=listed)])
    asset = build(saved)['assets'][SYMBOL]
    assert asset['events'][0]['stockStatus'] == expected_status
    capital_gaps = [g for g in asset['gaps'] if g['affects']=='capital']
    assert len(capital_gaps) == 1
    assert capital_gaps[0]['date'] == date.fromisoformat(f'{listed[:4]}-{listed[4:6]}-{listed[6:]}').isoformat()


def test_bulk_ledger_never_invokes_manual_company_resolution(saved,monkeypatch):
    import backend.tests.support.manual_dividend_resolution as manual
    def forbidden(*args,**kwargs):
        raise ValueError('manual resolution must not enter production')
    monkeypatch.setattr(manual,'resolve_announced_supersession',forbidden)
    assert build(saved)['sourceStatus']=='observed'
