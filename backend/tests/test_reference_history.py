from datetime import date
from decimal import Decimal
import pytest

def test_history_requires_exact_full_calendar_not_intersection():
    from app.portfolio.reference_history import normalize_index_history
    rows=[{'ts_code':'H00300.CSI','trade_date':'20261020','close':Decimal('100'),'sourceId':'raw:1'}]
    with pytest.raises(ValueError):normalize_index_history(rows,'H00300.CSI',('2026-10-20','2026-10-21'))

def test_history_rejects_duplicates_wrong_symbol_and_nonpositive():
    from app.portfolio.reference_history import normalize_index_history
    row={'ts_code':'H00300.CSI','trade_date':'20261020','close':Decimal('100'),'sourceId':'raw:1'}
    for values in ([row,row],[row|{'ts_code':'H00922.CSI'}],[row|{'close':0}],[row|{'close':True}]):
        with pytest.raises(ValueError):normalize_index_history(values,'H00300.CSI',('2026-10-20',))

def test_history_normalizes_sorted_prices_and_preserves_decimal():
    from app.portfolio.reference_history import normalize_index_history
    rows=[{'ts_code':'H00300.CSI','trade_date':'20261021','close':Decimal('102.25'),'sourceId':'raw:2'},
          {'ts_code':'H00300.CSI','trade_date':'20261020','close':Decimal('100.1'),'sourceId':'raw:1'}]
    assert normalize_index_history(rows,'H00300.CSI',('2026-10-20','2026-10-21'))==[{'date':'2026-10-20','price':100.1},{'date':'2026-10-21','price':102.25}]

def test_benchmark_dto_rejects_false_complete_and_future():
    from app.portfolio.reference_history import valid_benchmarks
    value={'H00300.CSI':dict(id='H00300.CSI',code='H00300.CSI',name='沪深300全收益',returnBasis='total-return',coverage={'start':'2026-10-20','end':'2026-10-21','complete':True},prices=[{'date':'2026-10-20','price':100},{'date':'2026-10-21','price':101}],sourceIds=['raw:1'])}
    assert valid_benchmarks(value,'2026-10-21')
    value['H00300.CSI']['coverage']['complete']=False
    assert not valid_benchmarks(value,'2026-10-21')

def test_analysis_allows_reference_only_as_benchmark():
    from backend.tests.test_portfolio_analysis import PublishedProvider
    from app.portfolio.analysis import analyze_portfolio
    from app.shared.errors import DataServiceError
    raw=PublishedProvider().raw
    raw['benchmarks']={'H00300.CSI':{'prices':raw['prices']['A']}}
    result=analyze_portfolio(raw,{'holdings':[{'assetId':'A','weight':1}],'benchmarkAssetId':'H00300.CSI'})
    assert result['nav']
    with pytest.raises(DataServiceError):analyze_portfolio(raw,{'holdings':[{'assetId':'H00300.CSI','weight':1}]})
