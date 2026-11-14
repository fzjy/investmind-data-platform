from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

def row(**changes):
    return SimpleNamespace(**(dict(id=1,source='chinamoney',series_id='cn.rate.govbond_10y',unit='%',
        observed_at=date(2026,10,20),released_at=datetime(2026,10,20,tzinfo=timezone.utc),
        revision='v1',value=Decimal('1.85'),source_url='https://www.chinamoney.com.cn/',valid_until=None)|changes))

def test_percent_conversion_and_date_visibility():
    from app.portfolio.treasury import build_treasury_reference
    result=build_treasury_reference([row(),row(id=2,observed_at=date(2026,10,22))],date(2026,10,21),'2026-01-01')
    assert len(result['points'])==1
    assert result['points'][0]['value']==0.0185
    assert result['points'][0]['sourceId']=='core.macro_observation:1'

def test_future_revision_and_unknown_units_are_not_used():
    from app.portfolio.treasury import build_treasury_reference
    assert not build_treasury_reference([row(released_at=datetime(2026,10,22,tzinfo=timezone.utc))],date(2026,10,21),'2026-01-01')['points']
    assert not build_treasury_reference([row(unit='bp')],date(2026,10,21),'2026-01-01')['points']

def test_missing_and_negative_rates_are_distinct():
    from app.portfolio.treasury import build_treasury_reference
    assert build_treasury_reference([],date(2026,10,21),'2026-01-01')['status']=='missing'
    assert build_treasury_reference([row(value=Decimal('-0.1'))],date(2026,10,21),'2026-01-01')['points'][0]['value']==-0.001

def test_dto_rejects_future_and_duplicate_evidence():
    from app.portfolio.treasury import build_treasury_reference,valid_treasury_reference
    result=build_treasury_reference([row()],date(2026,10,21),'2026-01-01')
    assert valid_treasury_reference(result,'2026-10-21')
    result['points'].append(dict(result['points'][0]))
    assert not valid_treasury_reference(result,'2026-10-21')

def test_bad_latest_unit_cannot_resurrect_an_older_revision():
    from app.portfolio.treasury import build_treasury_reference
    rows=[row(),row(id=2,unit='bp',revision='v2',released_at=datetime(2026,10,21,tzinfo=timezone.utc))]
    assert build_treasury_reference(rows,date(2026,10,21),'2026-01-01')['status']=='invalid'


def test_history_keeps_pre_window_and_expired_versions_and_root_forwards_them():
    from app.portfolio.normalizer import normalize_portfolio
    from app.portfolio.treasury import build_treasury_reference, valid_treasury_reference
    t1 = datetime(2026, 10, 21, 10, tzinfo=timezone.utc)
    rows = [row(observed_at=date(2026, 10, 19), id=0), row(valid_until=t1),
            row(id=2, revision='r2', released_at=t1, value=Decimal('2'))]
    model = build_treasury_reference(rows, date(2026, 10, 21), '2026-10-20')
    assert [p['value'] for p in model['points']] == [.02]
    assert [p['value'] for p in model['revisions']] == [.0185, .0185, .02]
    assert model['revisions'][1]['validUntil'] == t1.isoformat()
    assert valid_treasury_reference(model, model['asOf'])
    root = dict(assets=[], defaultState={}, prices={}, snapshotLabel=model['asOf'], storageKey='s', treasuryReference=model)
    assert normalize_portfolio(root)['treasuryReference'] == model
    legacy = {k: v for k, v in model.items() if k != 'revisions'}
    assert valid_treasury_reference(legacy, model['asOf'])
    model['revisions'].append(model['revisions'][1])
    assert not valid_treasury_reference(model, model['asOf'])
