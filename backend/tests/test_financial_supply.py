from datetime import date,datetime,timezone
from copy import deepcopy
import json,base64,hashlib
import pytest
from sqlalchemy import select
from app.db.models import JobCursor,PublishedSnapshot,SourcePayload
from backend.tests.test_dividend_evidence_capture import source,db_state


def module():
    from app.stocks import financial_supply
    return financial_supply


def test_preview_is_readonly_dynamic_and_keeps_old_request_contracts():
    f=source();before=db_state(f)
    r=module().capture(f,execute=False)
    assert r['status']=='planned' and r['requestCount']==0 and r['unitCount']==3
    assert db_state(f)==before


class Client:
    def __init__(self):self.calls=[]
    def set_response_observer(self,callback):self.callback=callback
    def call(self,api,params,fields):
        from app.connectors.tushare.contracts import TushareResponse
        self.calls.append((api,deepcopy(params),fields))
        rows=[]
        for end,ann in [('20241231','20250301'),('20250630','20250801')]:
            row=dict.fromkeys(fields);row.update(ts_code=params['ts_code'],end_date=end,ann_date=ann,f_ann_date=ann,report_type='1',comp_type='1',update_flag='0',revenue=100,oper_cost=60,accounts_receiv=12,n_cashflow_act=20)
            rows.append([row[k] for k in fields])
        data=dict(fields=list(fields),items=rows);body=json.dumps({'code':0,'data':data}).encode()
        self.callback(dict(statusCode=200,credentialsRedacted=False,bodyBase64=base64.b64encode(body).decode(),bodySha256=hashlib.sha256(body).hexdigest(),byteLength=len(body)))
        return TushareResponse.model_validate(data)


def test_budget_resumes_originals_and_reentry_never_refetches():
    f=source();c=Client()
    for pending in (True,True,False):
        r=module().capture(f,c,execute=True,max_http=1)
        assert r['requestCount']==1
        assert (r['status']=='pending')==pending
    before=db_state(f)
    assert module().capture(f,c,execute=True)['requestCount']==0
    assert len(c.calls)==3 and db_state(f)==before


def test_failed_unit_cannot_be_bypassed_with_new_base():
    f=source()
    class Broken(Client):
        def call(self,*args):self.calls.append(args);raise TimeoutError('private-key')
    c=Broken();r=module().capture(f,c,execute=True,max_http=1)
    assert r['status'] in ('failed','blocked')
    with f.begin() as s:s.get(PublishedSnapshot,'screener').version='new'
    assert module().capture(f,c,execute=True)['requestCount']==0
    assert len(c.calls)==1


def test_projection_has_explicit_metadata_and_never_leaks_future_or_parent():
    from app.stocks.derived.inputs import FinancialPoint
    points=[FinancialPoint(date(2025,6,30),date(2025,8,1),'1',{'revenue':100,'oper_cost':60},'600519.SH','core.income_statement','a'),
            FinancialPoint(date(2025,6,30),date(2025,8,1),'1',{'comp_type':'2'},'600519.SH','core.balance_sheet','bank'),
            FinancialPoint(date(2025,6,30),date(2025,9,1),'1',{'revenue':999},'600519.SH','core.income_statement','future'),
            FinancialPoint(date(2025,6,30),date(2025,8,1),'6',{'revenue':888},'600519.SH','core.income_statement','parent')]
    tables=module().statement_tables(points,date(2025,8,24))
    income=next(t for t in tables if t['kind']=='income')
    assert next(t for t in tables if t['kind']=='balance')['reportMetadata'][0]['companyType']=='2'
    assert income['reportMetadata'][0]['announcementDate']=='2025-08-01'
    assert next(r for r in income['rows'] if r['label']=='revenue')['values']==[100]
    assert next(r for r in income['rows'] if r['label']=='gross_profit')['values']==[40]


def test_same_disclosure_conflict_stays_missing_not_arbitrary_first():
    from app.stocks.derived.inputs import FinancialPoint
    points=[FinancialPoint(date(2025,6,30),date(2025,8,1),'1',{'revenue':v},'600519.SH','core.income_statement',str(v)) for v in (1,2)]
    t=module().statement_tables(points,date(2025,8,24))[0]
    assert next(r for r in t['rows'] if r['label']=='revenue')['values']==[None]
    assert t['reportMetadata'][0]['conflict'] is True


def test_same_actual_date_and_flag_use_latest_visible_announcement():
    from app.stocks.derived.inputs import FinancialPoint, visible_financial_rows

    period = date(2017, 12, 31)
    actual = date(2020, 6, 22)
    def point(source, announced, amount, flag='1'):
        return FinancialPoint(period, actual, '1',
            dict(ann_date=announced, update_flag=flag, free_cashflow=amount),
            '688180.SH', 'core.cashflow_statement', source)

    old = point('z-old', '20180424', -379576999.13)
    new = point('a-new', actual, -358440260.6)
    future = point('future', '20200623', 99)
    older_flag = point('old-flag', actual, 101, flag='0')
    for points in ([future, new, older_flag, old], [old, older_flag, new, future]):
        table = module().statement_tables(points, actual)[2]
        assert table['reportMetadata'][0]['sourceIds'] == ['a-new']
        assert table['reportMetadata'][0]['conflictFields'] == []
        assert next(row['values'] for row in table['rows'] if row['label'] == 'free_cashflow') == [-358440260.6]
        assert visible_financial_rows(points, actual) == (new,)
        assert module().statement_tables(points, date(2020, 6, 21))[2]['periods'] == []

    tied = module().statement_tables([old, new, point('same-date', '2020-06-22', -350000000)], actual)[2]
    assert tied['reportMetadata'][0]['conflictFields'] == ['free_cashflow']
    assert next(row['values'] for row in tied['rows'] if row['label'] == 'free_cashflow') == [None]


def test_original_row_lineage_is_not_a_financial_statement_item():
    from app.stocks.derived.inputs import FinancialPoint
    point=FinancialPoint(date(2025,6,30),date(2025,8,1),'1',{'revenue':100,'sourceId':'raw.source_payload:1:row:1','_sourceAsOf':'2025-08-24'},'600519.SH','core.income_statement','raw:1:1')
    table=module().statement_tables([point],date(2025,8,24))[0]
    assert {r['label'] for r in table['rows']}=={'revenue','gross_profit'}
    assert table['reportMetadata'][0]['sourceIds']==['raw:1:1']


def test_publication_changes_financial_reports_and_dependent_cards_only_and_reentry_is_no_change():
    from app.stocks.financial_runtime import publish
    f=source();c=Client();module().capture(f,c,execute=True)
    with f() as s:
        originals={p.product:deepcopy(p.payload) for p in s.scalars(select(PublishedSnapshot))}
    result=publish(f)
    assert result['status']=='completed',json.dumps(result)
    with f() as s:
        for p in s.scalars(select(PublishedSnapshot)):
            old=originals[p.product]
            if not p.product.startswith('stock-workbench:'):assert p.payload==old;continue
            current=deepcopy(p.payload);previous=deepcopy(old)
            for key in ('years','reportTypes','announcementDate','statements'):
                current['financial'].pop(key,None);previous['financial'].pop(key,None)
            from app.stocks.derived.workbench_cards import CARD_IDS
            for payload in (current, previous):
                metrics = payload['metrics']
                metrics.pop('cardInputSignature')
                metrics['cards'] = [c for c in metrics['cards'] if c.get('id') not in CARD_IDS]
            assert any(ref.startswith('raw.source_payload:') for card in p.payload['metrics']['cards']
                       for ref in card.get('calculation', {}).get('sourceIds', []))
            from app.stocks.derived.risk_inputs import load_risk_inputs
            from app.stocks.derived.workbench_risks import calculate_risks
            symbol = p.payload["symbol"]
            expected = calculate_risks(load_risk_inputs(s, [symbol], p.as_of.date())[symbol])
            assert current.pop("risks") == expected
            previous.pop("risks")
            assert current==previous
    assert publish(f)['status']=='no_change'


def test_paging_follows_saved_has_more_without_repeating_first_page():
    from app.connectors.tushare.contracts import TushareResponse
    class Pages(Client):
        def call(self,api,params,fields):
            self.calls.append((api,deepcopy(params),fields))
            offset=params.get('offset',0)
            row=dict.fromkeys(fields);row.update(ts_code=params['ts_code'],end_date='20241231' if not offset else '20231231',ann_date='20250301' if not offset else '20240301',f_ann_date='20250301' if not offset else '20240301',report_type='1',revenue=100)
            data=dict(fields=list(fields),items=[[row[k] for k in fields]],has_more=not bool(offset))
            body=json.dumps({'code':0,'data':data}).encode()
            self.callback(dict(statusCode=200,credentialsRedacted=False,bodyBase64=base64.b64encode(body).decode(),bodySha256=hashlib.sha256(body).hexdigest(),byteLength=len(body)))
            return TushareResponse.model_validate(data)
    f=source();c=Pages()
    for _ in range(6):result=module().capture(f,c,execute=True,max_http=1)
    assert result['status']=='completed' and result['unitCount']==6
    assert module().capture(f,c,execute=True)['requestCount']==0
    assert len({(a,json.dumps(p,sort_keys=True)) for a,p,_ in c.calls})==6


def test_bad_original_keeps_actual_physical_request_count():
    from app.connectors.tushare.contracts import TushareResponse
    class Bad(Client):
        def call(self,*args):
            response=super().call(*args)
            items=[list(r) for r in response.items];items[0][response.fields.index('end_date')]='20231231'
            return TushareResponse(fields=response.fields,items=tuple(tuple(r) for r in items))
    result=module().capture(source(),Bad(),execute=True,max_http=1)
    assert result['status']=='blocked' and result['requestCount']==1


def test_partial_capture_does_not_block_normal_workbench_build():
    from app.stocks.financial_runtime import tables_for_build
    f=source();module().capture(f,Client(),execute=True,max_http=1)
    assert tables_for_build(f,date(2026,8,24),('600519.SH',))=={}


def test_completed_old_batch_can_bind_new_price_day_without_restarting_all():
    from datetime import timedelta
    f=source();c=Client();module().capture(f,c,execute=True,max_http=1)
    with f.begin() as s:
        base=s.get(PublishedSnapshot,'screener');base.version='new-day';base.as_of+=timedelta(days=1)
    r=module().capture(f,c,execute=True)
    assert r['status']=='completed' and r['compatibleTarget'] is True and r['currentTarget'] is False
    points=module().load_points(f,date(2026,8,25),('600519.SH',))
    assert points and all(p.values['_sourceAsOf']=='2026-08-24' for p in points)
    assert module().capture(f,c,execute=True)['requestCount']==0


def test_legacy_core_anchor_does_not_shadow_target_lock_key():
    from app.ingestion.repositories import content_hash
    f=source();c=Client();m=module();m.capture(f,c,execute=True)
    # 模拟已保存的早期合同，保留原请求与原件，只移除后来才增加的Core签名。
    with f.begin() as s:
        cursors=s.scalars(select(JobCursor).where(JobCursor.job_name==m.JOB)).all()
        contract=next(r for r in cursors if r.cursor_key.startswith('contract:'))
        old_hash=content_hash(contract.cursor_value)
        legacy=dict(contract.cursor_value);legacy.pop('coreSignature')
        new_hash=content_hash(legacy)
        contract.cursor_value=legacy;contract.cursor_key='contract:'+new_hash
        for row in cursors:
            if row.cursor_key.startswith(old_hash+':'):row.cursor_key=new_hash+row.cursor_key[len(old_hash):]
    assert m.capture(f,c,execute=True)['status']=='completed'
    result=m.capture(f,c,execute=True)
    assert result['status']=='completed' and result['requestCount']==0
    assert len(c.calls)==3


def test_card_inputs_retain_prior_visible_revision_and_ignore_unfinished_or_duplicate_captures(monkeypatch):
    from app.connectors.tushare.contracts import TushareResponse
    from app.stocks.derived.inputs import load_metric_card_inputs

    class RevisionClient(Client):
        revised = False

        def call(self, api, params, fields):
            self.calls.append((api, deepcopy(params), fields))
            row = dict.fromkeys(fields)
            changed = self.revised and api == 'income'
            row.update(ts_code=params['ts_code'], end_date='20260630', ann_date='20260801',
                       f_ann_date='20261001' if changed else '20260801', report_type='1', comp_type='1',
                       update_flag='1' if changed else '0', basic_eps=2 if changed else 1)
            more = changed and 'offset' not in params
            data = dict(fields=list(fields), items=[] if 'offset' in params else [[row[k] for k in fields]],
                        has_more=more)
            body = json.dumps({'code': 0, 'data': data}).encode()
            self.callback(dict(statusCode=200, credentialsRedacted=False, bodyBase64=base64.b64encode(body).decode(),
                               bodySha256=hashlib.sha256(body).hexdigest(), byteLength=len(body)))
            return TushareResponse.model_validate(data)

    f = source()
    m = module()
    client = RevisionClient()
    symbol = '600519.SH'
    def income(day):
        with f() as s:
            data = load_metric_card_inputs(s, [symbol], date.fromisoformat(day))[symbol]
            return next(r for r in data['financials'] if r['table']=='income' and r['period']=='2026-06-30')
    assert m.capture(f, client, execute=True)['status'] == 'completed'
    old = income('2026-09-30')
    assert old['values']['basic_eps'] == 1
    client.revised = True
    monkeypatch.setattr(m, '_core_signature', lambda *_: 'revision')
    with f.begin() as s:
        base = s.get(PublishedSnapshot, 'screener')
        base.as_of = datetime(2026, 11, 1, tzinfo=timezone.utc)
        base.version = 'november'
    assert m.capture(f, client, execute=True, max_http=1)['status'] == 'pending'
    assert income('2026-11-01') == old
    assert m.capture(f, client, execute=True)['status'] == 'completed'
    assert income('2026-09-30') == old
    newer = income('2026-11-01')
    assert newer['values']['basic_eps'] == 2 and newer['announcedAt'] == '2026-10-01'
    monkeypatch.setattr(m, '_core_signature', lambda *_: 'new-contract-same-data')
    assert m.capture(f, client, execute=True)['status'] == 'completed'
    assert income('2026-11-01') == newer and income('2026-09-30') == old


def test_visible_core_change_selects_new_batch_without_overwriting_old(monkeypatch):
    f=source();c=Client();m=module();original=m.capture(f,c,execute=True)
    monkeypatch.setattr(m,'_core_signature',lambda *_:'changed-visible-core-facts')
    preview=m.capture(f,execute=False)
    assert preview['identity']!=original['identity']
    assert preview['completedCount']==0 and preview['requestCount']==0
    next_batch=m.capture(f,c,execute=True,max_http=1)
    assert next_batch['completedCount']==1 and next_batch['requestCount']==1
    with f() as s:
        assert len(s.scalars(select(SourcePayload)).all())==4
        assert len(s.scalars(select(JobCursor).where(JobCursor.cursor_key.like('contract:%'))).all())==2


def test_no_supplement_batch_does_not_require_market_only_pointer_stock_rows():
    from app.stocks.financial_runtime import tables_for_build
    f=source()
    with f.begin() as s:s.get(PublishedSnapshot,'screener').payload={}
    assert module().load_points(f,date(2026,8,24),('600519.SH',))==()
    assert tables_for_build(f,date(2026,8,24),('600519.SH',))=={}

def test_cashflow_contracts_share_full_fields_and_only_request_individual_stocks():
    from app.connectors.tushare.strategies import Dataset, SyncScope, TushareMode, build_request_plans
    from app.stocks.update.planning import plan_for

    symbols = ("600000.SH", "600519.SH")
    contract = dict(symbols=symbols, asOf="2026-11-12", policy=module().config())
    history = [r for r in module()._requests(contract) if r["api"] == "cashflow"]
    ordinary = build_request_plans(Dataset.CASHFLOW, TushareMode.STANDARD, SyncScope(symbols=symbols))
    for symbol, old, normal in zip(symbols, history, ordinary, strict=True):
        daily = plan_for(Dataset.CASHFLOW, {"ts_code": symbol, "period": "20260930"})
        assert normal.api_name == daily.api_name == "cashflow"
        assert normal.params["ts_code"] == daily.params["ts_code"] == old["params"]["ts_code"] == symbol
        assert set(old["fields"]) == set(normal.fields) == set(daily.fields)
        assert {"c_prepay_amt_borr", "eff_fx_flu_cash", "n_incr_pledge_loan", "net_dism_capital_add",
                "net_cash_rece_sec", "incl_cash_rec_saims", "n_depos_incr_fi", "pay_comm_insur_plcy"} <= set(daily.fields)


@pytest.mark.parametrize("failed", [False, True], ids=["pending-refresh", "failed-refresh"])
def test_daily_cashflow_reaches_workbench_during_history_refresh(failed):
    from app.db.models import CashflowStatement
    from app.stocks.financial_runtime import tables_for_build, publish

    factory = source()
    module().capture(factory, Client(), execute=True)
    with factory.begin() as session:
        session.add(CashflowStatement(ts_code="600519.SH", end_date=date(2026, 6, 30),
            ann_date=date(2026, 8, 1), report_type="1", update_flag="1",
            data={"net_cash_rece_sec": -100, "c_prepay_amt_borr": 0, "n_incr_pledge_loan": None}))
    reserved_values = []
    class Unavailable(Client):
        def call(self, *args):
            # 网络请求前已提交 reserved 单元，模拟同一时刻另一工作台只读构建。
            cash = next(t for t in tables_for_build(factory, date(2026, 8, 25), ("600519.SH",))["600519.SH"]
                        if t["kind"] == "cashflow")
            reserved_values.append(next(r["values"][-1] for r in cash["rows"] if r["label"] == "net_cash_rece_sec"))
            raise TimeoutError("offline")
    batch = module().capture(factory, Unavailable() if failed else Client(), execute=True, max_http=1)
    assert batch["status"] in ({"failed", "blocked"} if failed else {"pending"})
    assert reserved_values == ([-100] if failed else [])
    with pytest.raises(ValueError, match="FINANCIAL_UNRESOLVED_PRIOR_UNIT" if failed else "FINANCIAL_BATCH_INCOMPLETE"):
        module().load_points(factory, date(2026, 8, 24), ["600519.SH"], contract_identity=batch["identity"])
    def check(tables):
        table = next(t for t in tables if t["kind"] == "cashflow")
        assert table["periods"] == ["2024-09-30", "2024-12-31", "2025-06-30", "2025-09-30", "2026-06-30"]
        historical = next(m for m in table["reportMetadata"] if m["period"] == "2025-06-30")
        assert any(ref.startswith("raw.source_payload:") for ref in historical["sourceIds"])
        rows = {r["label"]: r["values"][-1] for r in table["rows"]}
        assert rows["net_cash_rece_sec"] == -100 and rows["c_prepay_amt_borr"] == 0
        assert rows["n_incr_pledge_loan"] is None and rows["recp_tax_rends"] is None
        assert set(table["reportMetadata"][-1]["requestedFields"]) == {
            "net_cash_rece_sec", "c_prepay_amt_borr", "n_incr_pledge_loan"}
    check(tables_for_build(factory, date(2026, 8, 25), ("600519.SH",))["600519.SH"])
    if failed:
        before = db_state(factory)
        assert publish(factory)["status"] == "blocked" and db_state(factory) == before
        return
    assert publish(factory)["status"] == "completed"
    with factory() as session:
        product = session.get(PublishedSnapshot, "stock-workbench:600519.SH")
        check(product.payload["financial"]["statements"]["tables"])

def test_only_disputed_source_fields_are_flagged_for_cashflow():
    from app.stocks.derived.inputs import FinancialPoint

    def point(fields, source):
        return FinancialPoint(date(2025, 6, 30), date(2025, 8, 1), "1", fields, "600519.SH",
                              "core.cashflow_statement", source)
    rows = [point(dict(c_inf_fr_operate_a=100, c_recp_borrow=20, c_paid_invest=None), "raw:1"),
            point(dict(c_recp_borrow=21, c_paid_invest=0, recp_tax_rends=10), "core:1")]
    cash = next(t for t in module().statement_tables(rows, date(2025, 8, 2)) if t["kind"] == "cashflow")
    meta = cash["reportMetadata"][0]
    assert meta["conflict"] and meta["conflictFields"] == ["c_paid_invest", "c_recp_borrow"]
    by_field = {row["label"]: row["values"][0] for row in cash["rows"]}
    assert by_field["c_inf_fr_operate_a"] == 100 and by_field["recp_tax_rends"] == 10
