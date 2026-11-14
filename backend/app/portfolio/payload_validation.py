from __future__ import annotations
from collections.abc import Mapping
from typing import Any
from datetime import date
from app.shared.payload_validation import _validate_record_list

def validate_portfolio(payload: Mapping[str, Any], missing: list[str]) -> None:
    _validate_record_list(payload.get("assets"), "assets", {"symbol": str, "name": str, "type": str}, missing)
    if 'attributionInputs' in payload:
        from app.portfolio.attribution_inputs import valid_attribution_inputs
        symbols = [row['id'] for row in payload.get('assets', []) if isinstance(row, Mapping) and row.get('type') == 'stock']
        if not valid_attribution_inputs(payload['attributionInputs'], symbols, str(payload.get('snapshotLabel'))):
            missing.append('attributionInputs')
    if 'benchmarks' in payload:
        from app.portfolio.reference_history import valid_benchmarks
        if not valid_benchmarks(payload['benchmarks'],payload.get('snapshotLabel')):missing.append('benchmarks')
    if 'treasuryReference' in payload:
        from app.portfolio.treasury import valid_treasury_reference
        if not valid_treasury_reference(payload['treasuryReference'],payload.get('snapshotLabel')):
            missing.append('treasuryReference')
    if 'dividendLedger' in payload:
        from app.portfolio.marts.portfolio_ledger import validate_dividend_ledger
        try:
            day = date.fromisoformat(str(payload.get('snapshotLabel')))
            symbols = tuple(r['id'] for r in payload.get('assets',[]) if isinstance(r,Mapping) and r.get('type')=='stock')
            missing.extend(validate_dividend_ledger(payload['dividendLedger'], symbols, day))
        except (ValueError, TypeError, KeyError):
            missing.append('dividendLedger')
    if 'dividendYieldInputs' in payload:
        from app.portfolio.dividend_yield_inputs import valid_yield_inputs
        if not valid_yield_inputs(payload['dividendYieldInputs'], payload.get('dividendLedger'), str(payload.get('snapshotLabel'))):
            missing.append('dividendYieldInputs')
