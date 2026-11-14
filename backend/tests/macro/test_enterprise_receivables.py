"""真实专用表只读适配：不重放写入、不接Raw-only或旧版本。"""
from dataclasses import replace
from datetime import UTC, date, datetime
import importlib
import importlib.util

import pytest
from sqlalchemy import select, text
from sqlalchemy import update

from backend.tests.macro.receivables_samples import AS_OF, POLICY, full_inputs, request_and_source, response_for
from backend.tests.macro.test_receivables_batch import setup_database


def loader():
    assert importlib.util.find_spec('app.macro.enterprise_receivables'), 'read-only supplement adapter is missing'
    return importlib.import_module('app.macro.enterprise_receivables').load_enterprise_receivables


def load(factory, data):
    return loader()(factory, data, AS_OF, POLICY, page_size=5000, timezone='Asia/Shanghai')


def seed(factory, store_module, models, *, value=123, persist=True, historical=True):
    data = full_inputs(); request, source = request_and_source(data)
    store = store_module.ReceivablesStore(factory)
    attempt = store.reserve(request, 'a' * 64, 'controlled', 5000)
    store.receive(attempt, response_for(source, value))
    if persist:
        store.recover(request, source, 5000, persist=True)
    if historical:
        # 仅受控SQLite夹具时间；业务验收禁止改时间。
        when = datetime(2024, 5, 1, tzinfo=UTC)
        with factory.begin() as session:
            a = session.get(models.ReceivableAttempt, attempt); a.created_at = a.finished_at = when
            session.get(models.ReceivableResponse, attempt).captured_at = when
            if persist:
                session.get(models.ReceivableSupplement, request['requestKey']).created_at = when
    return data, request, source


@pytest.mark.parametrize('value,status', [(0, 'matched'), (None, 'returned-null')])
def test_verified_result_loaded_without_database_write(tmp_path, value, status):
    _, store, models, url, engine, factory = setup_database(tmp_path)
    try:
        data, _, source = seed(factory, store, models, value=value)
        before = (tmp_path / 'supplement.sqlite').read_bytes()
        result = load(factory, data)
        assert len(result.receivables) == 1 and result.receivables[0].status == status
        assert result.receivables[0].source_key == source.source_key
        assert result.receivables[0].value == ('0' if value == 0 else None)
        assert (tmp_path / 'supplement.sqlite').read_bytes() == before and result.financials == data.financials
    finally:
        engine.dispose()


def test_supplement_provenance_ref_resolves_to_real_persisted_request_key(tmp_path):
    from app.macro.enterprise import build_enterprise_analysis
    _, store, models, _, engine, factory = setup_database(tmp_path)
    try:
        data, request, _ = seed(factory, store, models)
        result = build_enterprise_analysis(load(factory, data), AS_OF, POLICY)
        refs = [ref for q in result['cards'][2]['quality'] for ref in q['sourceRefs']
                if ref['table'] == 'receivable-supplement']
        assert refs and all(ref['sourceKey'] == 'core.enterprise_receivable_supplement:' + request['requestKey'] for ref in refs)
    finally:
        engine.dispose()


def test_profit_only_revision_cannot_clear_valid_white_strip_for_fixed50(tmp_path):
    from app.macro.enterprise import EnterpriseInputs, EnterprisePolicy, SecurityInput, IndustryInput, build_enterprise_analysis
    from app.macro.receivables_preview import preview_receivables
    from app.stocks.universes import UniverseResolver
    _, store_module, models, _, engine, factory = setup_database(tmp_path)
    try:
        symbols = tuple(UniverseResolver().resolve('representative-50').symbols)
        template = [r for r in full_inputs().financials if r.symbol == '000001.SZ']
        rows = tuple(replace(r, symbol=symbol, source_key=f'{r.table}:{symbol}:{r.end_date.year}',
            values={**r.values, **({'inventories': 0} if r.table == 'balance' else {})})
            for symbol in symbols for r in template)
        data = EnterpriseInputs('controlled-fixed50', symbols,
            tuple(SecurityInput(s, date(2020,1,1)) for s in symbols), rows,
            tuple(IndustryInput(s,'801120.SI',date(2020,1,1),None) for s in symbols))
        policy = EnterprisePolicy()
        requests = preview_receivables(data, AS_OF, policy, 8, 100)['requests']
        assert len(requests) == 100
        store = store_module.ReceivablesStore(factory)
        for request in requests:
            source = next(r for r in rows if r.source_key == request['coreBinding']['sourceKey'])
            attempt = store.reserve(request, 'c'*64, 'controlled-profit-revision', 5000)
            store.receive(attempt, response_for(source, 100 if source.end_date.year == 2023 else 150))
            store.recover(request, source, 5000, persist=True)
        with factory.begin() as session:
            when = datetime(2024,5,1,tzinfo=UTC)
            session.execute(update(models.ReceivableAttempt).values(created_at=when, finished_at=when))
            session.execute(update(models.ReceivableResponse).values(captured_at=when))
            session.execute(update(models.ReceivableSupplement).values(created_at=when))
        revised = replace(data, financials=tuple(replace(r,values={**r.values,'n_income_attr_p':0})
            if r.table == 'income' and r.end_date.year == 2023 else r for r in rows))
        assert preview_receivables(revised,AS_OF,policy,8,100)['requests'] == []
        loaded = loader()(factory,revised,AS_OF,policy,page_size=5000,timezone='Asia/Shanghai')
        result = build_enterprise_analysis(loaded,AS_OF,policy)
        assert result['cards'][0]['points'] == result['cards'][1]['points'] == []
        assert len(loaded.receivables) == 100
        point = result['cards'][2]['points'][-1]
        assert (point['primary'],point['secondary'],point['result'],point['sampleCount']) == (50,10,40,50)
    finally:
        engine.dispose()


def test_raw_only_is_not_materialized_and_revision_does_not_use_old_supplement(tmp_path):
    _, store, models, _, engine, factory = setup_database(tmp_path)
    try:
        data, request, source = seed(factory, store, models, persist=False)
        assert load(factory, data).receivables == ()
        with factory() as session:
            assert session.get(models.ReceivableSupplement, request['requestKey']) is None
        store.ReceivablesStore(factory).recover(request, source, 5000, persist=True)
        changed = replace(data, financials=tuple(replace(r, values={**r.values, 'money_cap': 101})
            if r.source_key == source.source_key else r for r in data.financials))
        assert load(factory, changed).receivables == ()
    finally:
        engine.dispose()


def test_future_capture_keeps_explicit_not_yet_visible_evidence(tmp_path):
    _, store, models, _, engine, factory = setup_database(tmp_path)
    try:
        data, _, _ = seed(factory, store, models, historical=False)
        result = load(factory, data)
        assert result.receivables[0].available_date > AS_OF
    finally:
        engine.dispose()


def test_corrupted_stored_response_rejected_not_silently_cleared(tmp_path):
    _, store, models, _, engine, factory = setup_database(tmp_path)
    try:
        data, _, _ = seed(factory, store, models)
        with factory.begin() as session:
            session.scalar(select(models.ReceivableResponse)).response_hash = 'b' * 64
        with pytest.raises(ValueError, match='ENTERPRISE_EVIDENCE_INVALID'):
            load(factory, data)
    finally:
        engine.dispose()


@pytest.mark.parametrize('all_missing', [True, False])
def test_old_schema_compatible_but_partial_schema_refuses(tmp_path, all_missing):
    _, _, _, _, engine, factory = setup_database(tmp_path)
    try:
        with factory.begin() as session:
            session.execute(text('DROP TABLE enterprise_receivable_supplement'))
            if all_missing:
                session.execute(text('DROP TABLE enterprise_receivable_response'))
                session.execute(text('DROP TABLE enterprise_receivable_attempt'))
        if all_missing:
            assert load(factory, full_inputs()).receivables is None
        else:
            with pytest.raises(ValueError, match='ENTERPRISE_SUPPLEMENT_SCHEMA_REQUIRED'):
                load(factory, full_inputs())
    finally:
        engine.dispose()
