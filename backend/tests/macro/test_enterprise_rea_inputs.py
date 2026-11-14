"""真实SQLite证据消费：损坏拒绝、Raw-only不重放、全过程只读。"""
from dataclasses import replace
from datetime import UTC, date, datetime
import importlib
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select, text

from app.macro.enterprise import EnterprisePolicy
from backend.tests.macro.test_rea_input_store import database, response_for
from backend.tests.macro.test_rea_input_preview import data, preview
from backend.tests.macro.test_enterprise_rea import metric
from app.macro.enterprise_contract import public_enterprise_analysis
from backend.tests.macro.test_enterprise_cli import database as enterprise_database


AS_OF = date(2024, 8, 1)
POLICY = EnterprisePolicy(min_samples=1)


def stage(database, *, raw_only=False, changes=None, inputs=None):
    module, models, _, _, factory = database
    inputs = inputs or data(2)
    plan = preview(inputs)
    store = module.ReaInputStore(factory)
    store.freeze(plan, inputs, 5000)
    for request in plan['requests']:
        source = next(r for r in inputs.financials if r.source_key == request['coreBinding']['sourceKey'])
        store.reserve(request, plan['planFingerprint'], 'consumer-test', 5000)
        values = {f: source.end_date.month * (2 if source.end_date.year == 2024 else 1)
                  for f in request['missingFields']}
        store.receive(request['requestKey'], response_for(request, source, {**values, **(changes or {})}))
        if not raw_only:
            store.recover(request, source, 5000, persist=True)
    with factory.begin() as session:
        for model in (models.ReaInputAttempt, models.ReaInputResponse, models.ReaInputSupplement):
            for row in session.scalars(select(model)):
                if model is models.ReaInputAttempt:
                    row.finished_at = datetime(2024, 7, 20, tzinfo=UTC)
                elif model is models.ReaInputResponse:
                    row.captured_at = datetime(2024, 7, 20, tzinfo=UTC)
                else:
                    row.created_at = datetime(2024, 7, 20, tzinfo=UTC)
    return inputs, plan


def load(database, inputs, *, page_size=5000):
    assert importlib.util.find_spec('app.macro.enterprise_rea_inputs'), 'REA readonly consumer is missing'
    return importlib.import_module('app.macro.enterprise_rea_inputs').load_enterprise_rea_inputs(
        database[-1], inputs, AS_OF, POLICY, page_size=page_size, timezone='Asia/Shanghai')


def test_persisted_fields_are_readonly_and_only_change_rea(database):
    inputs, _ = stage(database)
    path = Path(database[2].removeprefix('sqlite:///'))
    before = path.read_bytes()
    loaded = load(database, inputs)
    assert len(loaded.rea_fields) == 40  # 每股2余额期×2字段+4现金流期×4字段。
    assert loaded.financials == inputs.financials and path.read_bytes() == before
    old = public_enterprise_analysis(inputs, AS_OF, POLICY, rea_enabled=True)
    result = public_enterprise_analysis(loaded, AS_OF, POLICY, rea_enabled=True)
    assert {k: v for k, v in result.items() if k != 'rea'} == {k: v for k, v in old.items() if k != 'rea'}
    assert metric(old['rea'], 'capacity')['points'] == []
    point = metric(result['rea'], 'capacity')['points'][-1]
    # 两股当季收入800/期末固定资产24，上年同季400/12；周转比变化0。
    assert point['amounts'] == {'a': 800, 'b': 24, 'oldA': 400, 'oldB': 12}
    assert point['value'] == 0


def test_raw_without_persisted_result_is_not_promoted_by_readonly_consumer(database):
    inputs, _ = stage(database, raw_only=True)
    path = Path(database[2].removeprefix('sqlite:///'))
    before = path.read_bytes()
    assert load(database, inputs).rea_fields == ()
    assert path.read_bytes() == before


def test_normal_loader_and_audit_consume_the_same_persisted_evidence(enterprise_database):
    import json
    from zoneinfo import ZoneInfo
    from typer.testing import CliRunner
    from app.cli.main import app
    from app.config import Settings
    from app.db.models import IncomeStatement, BalanceSheet, CashflowStatement
    from app.macro.enterprise_contract import enterprise_loader
    from app.macro.enterprise_repository import load_enterprise_inputs, resolve_enterprise_selection
    from app.macro import rea_input_store
    from app.db.models import enterprise_rea_inputs as models
    path, factory = enterprise_database
    factory.configure(expire_on_commit=False)
    settings = Settings.from_env()
    with factory.begin() as session:
        for model in (IncomeStatement, BalanceSheet, CashflowStatement):
            session.query(model).delete()
        for row in data(2).financials:
            if row.symbol != '000001.SZ': continue
            model = {'income': IncomeStatement, 'balance': BalanceSheet, 'cashflow': CashflowStatement}[row.table]
            session.add(model(ts_code=row.symbol, end_date=row.end_date, ann_date=row.ann_date,
                f_ann_date=row.f_ann_date, report_type=row.report_type, comp_type=row.comp_type,
                update_flag=row.update_flag, data=dict(row.values)))
    inputs = load_enterprise_inputs(factory, resolve_enterprise_selection(factory, settings))
    stage((rea_input_store, models, None, None, factory), inputs=inputs)
    before = path.read_bytes()
    today = datetime.now(ZoneInfo(settings.sync_timezone)).date()
    normal = enterprise_loader(factory, settings)(today)
    assert normal['rea']['version']=='macro-enterprise-rea-v2'
    point = next((p for p in metric(normal['rea'], 'capacity')['points'] if p['period'] == '2024-06-30'), None)
    assert point is not None, 'normal publishing loader has not consumed REA evidence'
    assert point['sampleCount'] == 1 and point['amounts']['b'] == 12
    cli = CliRunner().invoke(app, ['audit-macro-enterprise'])
    assert cli.exit_code == 0, cli.stdout
    audit = next(json.loads(line) for line in cli.stdout.splitlines() if line.startswith('{'))
    assert audit['rea'] == normal['rea']
    assert audit['industryComparison'] == normal['industryComparison']
    assert normal['industryComparison']['groups'][0]['name'] == '消费'
    assert path.read_bytes() == before


@pytest.mark.parametrize('damage', ['raw', 'result', 'plan', 'page'])
def test_corrupt_evidence_and_page_policy_changes_fail_closed(database, damage):
    inputs, plan = stage(database)
    _, models, _, _, factory = database
    key = plan['requests'][0]['requestKey']
    with factory.begin() as session:
        if damage == 'raw': session.get(models.ReaInputResponse, key).response_hash = '0' * 64
        elif damage == 'result': session.get(models.ReaInputSupplement, key).outcome = {'status': 'matched', 'fields': {}}
        elif damage == 'plan': session.get(models.ReaInputPlan, plan['planFingerprint']).snapshot_hash = '0' * 64
    with pytest.raises(ValueError):
        load(database, inputs, page_size=4000 if damage == 'page' else 5000)


def test_changed_core_pool_and_ambiguous_metadata_never_reuse_old_evidence(database):
    inputs, _ = stage(database)
    assert load(database, replace(inputs, universe_version='new-pool')).rea_fields == ()
    changed = replace(inputs, financials=tuple(replace(r, values={**r.values, 'money_cap': 999})
        if r.table == 'balance' else r for r in inputs.financials))
    assert {e.field for e in load(database, changed).rea_fields} == {
        'depr_fa_coga_dpba', 'amort_intang_assets', 'lt_amort_deferred_exp', 'c_paid_to_for_empl'}
    # 并列公告可见日期相同但原始公告元数据不同，应拒用，不随机选一条。
    rows = tuple(replace(r, f_ann_date=r.ann_date, source_key=r.source_key + ':tie')
                 for r in inputs.financials if r.table == 'balance')
    ambiguous = replace(inputs, financials=inputs.financials + rows)
    assert load(database, ambiguous).rea_fields == ()


def test_latest_receipt_date_and_returned_null_remain_explicit_gaps(database):
    inputs, _ = stage(database, changes={'fix_assets': None})
    _, models, _, _, factory = database
    with factory.begin() as session:
        for row in session.scalars(select(models.ReaInputSupplement)):
            row.created_at = datetime(2024, 8, 1, 17, tzinfo=UTC)
    loaded = load(database, inputs)
    assert {e.available_date for e in loaded.rea_fields} == {date(2024, 8, 2)}
    assert {e.status for e in loaded.rea_fields if e.field == 'fix_assets'} == {'returned-null'}
    result = public_enterprise_analysis(loaded, AS_OF, POLICY, rea_enabled=True)
    assert metric(result['rea'], 'capacity')['points'] == []
    assert set(metric(result['rea'], 'capacity')['quality'][-1]['excluded'].values()) == {'rea-supplement-not-visible'}


def test_absent_schema_is_compatible_but_partial_schema_and_duplicate_source_fail(database):
    inputs = data()
    assert load(database, inputs).rea_fields == ()
    with pytest.raises(ValueError):
        load(database, replace(inputs, financials=inputs.financials + inputs.financials[:1]))
    with database[-1].begin() as session:
        session.execute(text('DROP TABLE enterprise_rea_supplement'))
    with pytest.raises(ValueError, match='REA_SUPPLEMENT_SCHEMA_REQUIRED'):
        load(database, inputs)
    with database[-1].begin() as session:
        for table in ('enterprise_rea_response', 'enterprise_rea_attempt', 'enterprise_rea_plan'):
            session.execute(text('DROP TABLE ' + table))
    assert load(database, inputs).rea_fields == ()


def test_renewal_estimation_reads_prior_fy_supplement_without_modifying_core(database):
    from app.macro.rea_input_store import ReaInputStore
    from app.macro.rea_input_preview import rea_input_request
    inputs=data(1)
    original=next(r for r in inputs.financials if r.table=='cashflow')
    annual=replace(original,end_date=date(2022,12,31),ann_date=date(2023,4,1),source_key='prior-fy')
    inputs=replace(inputs,financials=inputs.financials+(annual,),renewal_estimation_enabled=True)
    request=rea_input_request(annual,inputs.universe_version)
    # 冻结与消费使用同一完整Core见证；不用新HTTP或消费时重放Raw。
    # 采用既有预览器以FY作为输出生成合法冻结计划。
    plan=preview(inputs,as_of=date(2023,5,1),quarters=2)
    store=ReaInputStore(database[-1]);store.freeze(plan,inputs,5000)
    assert request['requestKey'] in {r['requestKey'] for r in plan['requests']}
    store.reserve(request,plan['planFingerprint'],'prior-fy-test',5000)
    store.receive(request['requestKey'],response_for(request,annual,dict.fromkeys(request['missingFields'],12)))
    store.recover(request,annual,5000,persist=True)
    from app.macro.enterprise_rea_inputs import load_enterprise_rea_inputs
    def one_quarter(value):
        return load_enterprise_rea_inputs(database[-1],value,AS_OF,EnterprisePolicy(min_samples=1,max_quarters=1),
            page_size=5000,timezone='Asia/Shanghai')
    assert not one_quarter(replace(inputs,renewal_estimation_enabled=False)).rea_fields
    path=Path(database[2].removeprefix('sqlite:///'));before=path.read_bytes()
    loaded=one_quarter(inputs)
    assert {e.field for e in loaded.rea_fields if e.source_key=='prior-fy'}==set(request['missingFields'])
    assert loaded.financials==inputs.financials
    assert path.read_bytes()==before
