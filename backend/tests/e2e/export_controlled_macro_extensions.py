"""供TS读取实际Python新合同；仅内存受控观测，不是供应商验收。"""
import json

from app.macro.extensions.normalize import normalize_extension_response
from app.macro.extensions.view import _empty, build_extension_view
from backend.tests.macro.test_extension_evidence import fx, gdp
from backend.tests.macro.test_extension_plan import config
from app.macro.extensions.plan import build_extension_plan
from backend.tests.macro.test_extension_store import NOW
import tempfile
from pathlib import Path
from backend.tests.macro.test_extension_vix import VIX_NOW, vix_config, vix_rows
from backend.tests.macro.test_fiscal_view import controlled_fiscal_view


def main():
    sources = {key: _empty(key) for key in ('usdcnh', 'nominalGdp')}
    with tempfile.TemporaryDirectory() as folder:
        policies = config(Path(folder))
        for group, response in [('usdcnh', fx()), ('nominalGdp', gdp())]:
            evidence = normalize_extension_response(build_extension_plan(group, policies, NOW), response)
            sources[group].update(status='available', reason=None, executionState='completed',
                retrievedAt=NOW.isoformat(), evidenceFingerprint=evidence['evidenceFingerprint'],
                points=[{'period': p['period'], 'value': p['value'],
                    'reportedRealYoy': p['diagnostics'].get('reportedRealYoy'), 'sourceRow': p['sourceRef']['row']}
                    for p in evidence['observations']])
    result = {'full': build_extension_view({'sources': sources}, [{'month': '2026-04-01', 'value': 5.5}]),
        'missing': build_extension_view({'sources': {key: _empty(key) for key in sources}})}
    with tempfile.TemporaryDirectory() as folder:
        evidence = normalize_extension_response(build_extension_plan('vix', vix_config(Path(folder)), VIX_NOW), vix_rows())
        vix = _empty('vix')
        vix.update(status='available', reason=None, executionState='completed', retrievedAt=VIX_NOW.isoformat(),
            evidenceFingerprint=evidence['evidenceFingerprint'], points=[{'period': p['period'], 'value': p['value'],
                'reportedRealYoy': None, 'sourceRow': p['sourceRef']['row']} for p in evidence['observations']])
        result['vix'] = build_extension_view({'sources': {**sources, 'vix': vix}}, [{'month': '2026-04-01', 'value': 5.5}])
        result['fiscal'] = build_extension_view({'sources': {**sources, 'vix': vix}, 'fiscal': controlled_fiscal_view(Path(folder))}, [{'month': '2026-04-01', 'value': 5.5}])
        from backend.tests.e2e.export_controlled_official_macro import cases
        result['official'] = build_extension_view({'sources': {**sources, 'vix': vix}, 'fiscal': controlled_fiscal_view(Path(folder)),
            'official': cases()['full']}, [{'month': '2026-04-01', 'value': 5.5}])
        result['officialWithoutFiscal'] = build_extension_view({'sources': {**sources, 'vix': vix},
            'official': cases()['full']}, [{'month': '2026-04-01', 'value': 5.5}])
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == '__main__':
    main()
