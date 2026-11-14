from copy import deepcopy
from datetime import date

import pytest

from backend.tests.test_portfolio_rebuild import source
from backend.tests.marts.test_content_runtime import state


def valid():
    return {'asOf':'2026-08-24','sourceBaseVersion':'source-parent','captureIdentity':'source-capture',
        'sourceStatus':'observed','basis':'observed-supplier-events','historyComplete':False,
        'assets':{'600519.SH':{'status':'observed','reason':'observed only','coverageStart':'2026-08-24','coverageEnd':'2026-08-24',
            'events':[],'issues':[],'gaps':[]}}}


def test_common_builder_and_explicit_rebuild_both_keep_ledger(monkeypatch):
    import app.portfolio.marts.portfolio_ledger as module
    from app.portfolio.marts.portfolio import PortfolioMartBuilder
    from app.portfolio.marts.portfolio_runtime import rebuild_portfolio
    from app.stocks.derived.inputs import CoreSnapshotRepository
    f=source();calls=[]
    def load(factory,symbols,as_of,price_starts):
        calls.append((symbols,as_of));return valid()
    monkeypatch.setattr(module,'load_portfolio_dividend_ledger',load)
    snapshot=CoreSnapshotRepository(f).load(date(2026,8,24),('600519.SH',))
    assert PortfolioMartBuilder(f).build(snapshot,'test').payload['dividendLedger']==valid()
    before=state(f)
    report=rebuild_portfolio(f,dry_run=False)
    assert report['status']=='completed' and report['promotedProducts']==['portfolio']
    after=state(f)
    assert after['pointers']['portfolio'][2]['dividendLedger']==valid()
    for p,v in before['pointers'].items():
        if p!='portfolio':assert after['pointers'][p]==v
    assert rebuild_portfolio(f,dry_run=False)['status']=='no_change'
    assert calls and all(c[0]==('600519.SH',) for c in calls)


@pytest.mark.parametrize('change', ['future','scope','nan','complete','bad-event','changed-source'])
def test_bad_ledger_cannot_promote_or_change_old_products(monkeypatch,change):
    import app.portfolio.marts.portfolio_ledger as module
    from app.portfolio.marts.portfolio_runtime import rebuild_portfolio
    f=source();before=state(f);value=valid()
    if change=='future':value['asOf']='2027-01-01'
    if change=='scope':value['assets']['OTHER']=deepcopy(value['assets']['600519.SH'])
    if change=='nan':value['assets']['600519.SH']['events']=[{'stockRatio':float('nan')}]
    if change=='complete':value['historyComplete']=True
    if change=='bad-event':value['assets']['600519.SH']['events']=[{'id':'broken'}]
    if change=='changed-source':value['sourceStatus']='base_changed'
    monkeypatch.setattr(module,'load_portfolio_dividend_ledger',lambda *args:value)
    report=rebuild_portfolio(f,dry_run=False)
    assert report['status']=='blocked' and state(f)==before


def test_without_capture_returns_missing_state_and_never_fixture():
    from app.portfolio.marts.portfolio_runtime import rebuild_portfolio
    f=source()
    report=rebuild_portfolio(f,dry_run=False)
    assert report['status']=='completed'
    model=state(f)['pointers']['portfolio'][2]['dividendLedger']
    assert model['sourceStatus']=='not_captured' and model['historyComplete'] is False
    assert model['assets']['600519.SH']['events']==[]


def test_yield_publication_calculates_current_model_from_inputs_instead_of_saved_value():
    from app.db.models import MetricValue
    from sqlalchemy import select
    from app.portfolio.marts.portfolio_runtime import rebuild_portfolio
    factory = source()
    with factory.begin() as session:
        model = session.scalar(select(MetricValue).where(MetricValue.metric_id=='workbenchNtmDividendYield'))
        model.value, model.quality, model.missing_fields = 4, 'ok', []
    assert rebuild_portfolio(factory, dry_run=False)['status'] == 'completed'
    payload = state(factory)['pointers']['portfolio'][2]
    inputs = payload['dividendYieldInputs']['600519.SH']
    assert inputs['querySource'] is None
    model = inputs['models']['2026-08-24']
    assert model['rate'] is None and model['reason']
    assert model['source'].startswith('calculated.metric:workbenchNtmDividendYield:v2:')
    assert payload['dividendLedger']['assets']['600519.SH']['coverageStart'] == '2025-08-24'


def test_fiscal_completion_requires_all_payments_and_visible_evidence():
    from app.portfolio.dividend_yield_inputs import fiscal_summary, valid_fiscal_years
    paid = dict(status='paid', payDate='2025-06-03', announcedAt='2025-05-01',
                recordDate='2025-06-02', cashPerShare='0.5', sourceIds=['s'])
    row = dict(covered=True, zeroConfirmed=False, events=[paid], observations=[
        dict(ann_date='2025-05-01', imp_ann_date='2025-05-30', sourceId='s')])
    fiscal = fiscal_summary(row)
    assert fiscal['completeAt'] == fiscal['payments'][0]['knownAt'] == '2025-06-03'
    assert valid_fiscal_years({'2024': fiscal}, '2025-06-03')
    assert not valid_fiscal_years({'2024': fiscal}, '2025-06-02')
    assert fiscal_summary(row | {'events': [paid, dict(status='pending')]})['completeAt'] is None
    assert fiscal_summary(row | {'covered': False})['completeAt'] is None
