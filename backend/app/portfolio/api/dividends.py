"""跨域公开能力；调用方不穿透领域内部文件。"""
from app.portfolio.dividend_contract import _date as distribution_date, resolve_distribution_evidence, select_legacy_dividend_rows
from app.portfolio.dividend_ledger import _original_rows as original_dividend_rows, build_dividend_ledger
from app.portfolio.dividend_report import DIVIDEND_FIELDS, _metadata as dividend_metadata
from app.portfolio.evidence import verified_dividend_rows, dividend_query_coverage
__all__ = ['distribution_date', 'resolve_distribution_evidence', 'select_legacy_dividend_rows', 'original_dividend_rows', 'build_dividend_ledger', 'DIVIDEND_FIELDS', 'dividend_metadata', 'verified_dividend_rows', 'dividend_query_coverage']
