from datetime import UTC, datetime
from app.acquisition.adapters.markets import record_market
from app.acquisition.contracts import StateKey
from app.acquisition.repository import StateRepository


def test_etf_price_and_shares_are_independent(factory):
    now = datetime.now(UTC)
    code = '512880.SH'
    with factory.begin() as s:
        record_market(s,'fund_daily',code,{'trade_date':'20260101'}, now, batch_id='p')
        record_market(s,'fund_share',code,{'trade_date':'20260101'}, now, error='SOURCE_FAILED')
        keys = tuple(StateKey(e,'','instrument',code) for e in ('etf-daily','etf-shares'))
        values = StateRepository(s).read(keys)
        assert [values[k].status for k in keys] == ['success','failed']
