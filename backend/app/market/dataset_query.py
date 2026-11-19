"""读取 Tushare 已保存的指数和 ETF 行情。交易日以市场当地业务日期为准。"""
from sqlalchemy import select
from app.db.models import MarketDailyQuote


def market_quote_statement(asset_type, start, end, *, asset_code=None, after=None):
    quote = MarketDailyQuote
    statement = select(quote).where(quote.provider == "tushare", quote.asset_type == asset_type,
                                    quote.trade_date.between(start, end))
    if asset_code is not None:
        statement = statement.where(quote.asset_code == asset_code)
    if after is not None:
        statement = statement.where(quote.trade_date > after)
    return statement.order_by(quote.trade_date, quote.asset_code)
