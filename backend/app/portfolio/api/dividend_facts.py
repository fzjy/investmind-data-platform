"""供领域读取分红捕获及裁决，独立于产品发布。"""
from app.portfolio.dividend_contract import _date as distribution_date, resolve_distribution_evidence
from app.portfolio.dividend_fields import DIVIDEND_FIELDS, _metadata as dividend_metadata
from app.portfolio.evidence import captured_dividend_history
from app.portfolio.security_mapping import security_aliases

__all__ = ['distribution_date', 'resolve_distribution_evidence', 'DIVIDEND_FIELDS',
           'dividend_metadata', 'captured_dividend_history', 'security_aliases']
