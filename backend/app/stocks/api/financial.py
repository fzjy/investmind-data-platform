"""跨域公开能力；调用方不穿透领域内部文件。"""
from app.stocks.derived.inputs import CoreSnapshot, load_metric_card_inputs, _years_before as years_before
from app.stocks.derived.workbench_cards import CARD_VERSION, calculate_metric_cards
from app.stocks.derived.metric_dividends import dividend_years, load_dividend_years
from app.stocks.derived.attribution import load_attribution_earnings
__all__ = ['CoreSnapshot', 'years_before', 'CARD_VERSION', 'calculate_metric_cards',
           'dividend_years', 'load_dividend_years', 'load_metric_card_inputs', 'load_attribution_earnings']
