"""真实Builder/发布器/SQLite；边界只注入已校验的扩展输入。"""
from copy import deepcopy
from dataclasses import replace
from datetime import date

from sqlalchemy import select

from app.db.models import MacroObservation
from app.macro.jobs.macro_publish import MacroPublishJob
from app.macro.extensions.view import _empty, build_extension_view
from app.marts.dto import validate_product_payload
from app.macro.marts.macro import MacroMartBuilder, MacroRepository
from app.macro.marts.macro_quality import evaluate_macro_quality
from backend.tests.jobs.test_macro_publish import AS_OF, CATALOG, setup


def inputs():
    return {'sources': {key: _empty(key) for key in ('usdcnh', 'nominalGdp')}}


def test_extension_loader_runs_once_for_pair_and_reentry_does_not_promote():
    engine, factory, _, history, publisher, statuses = setup(20)
    try:
        with factory.begin() as session:
            session.scalar(select(MacroObservation).where(
                MacroObservation.series_id == 'cn.gdp.real_yoy')).observed_at = date(2026, 7, 1)
        calls = []
        def loader(cutoff):
            calls.append(cutoff)
            return inputs()
        builder = MacroMartBuilder(MacroRepository(factory), catalog=CATALOG, extension_loader=loader)
        job = MacroPublishJob(builder, publisher, history)
        first = job.publish('macro-run', AS_OF, CATALOG, statuses)
        assert first.status == 'completed' and len(calls) == 1
        pair = publisher.current_macro_pair()
        assert len(pair) == 2
        assert pair[0].payload['extensionInsights'] == pair[1].payload['extensionInsights']
        assert len(pair[0].payload['seriesStatus']) == 20
        second = job.publish('macro-run', AS_OF, CATALOG, statuses, reuse_unchanged=True)
        assert second.promoted_products == () and len(calls) == 2
        assert next(q for q in history.quality if q.name == 'macro-extensions-source-bound').passed
    finally:
        engine.dispose()


def test_source_quality_and_dto_reject_missing_or_changed_contract_without_altering_legacy():
    engine, factory, _, _, _, _ = setup(20)
    try:
        builder = MacroMartBuilder(MacroRepository(factory), catalog=CATALOG)
        legacy = builder.build(AS_OF, 'legacy')
        assert all('extensionInsights' not in c.payload for c in legacy)
        expected = build_extension_view(inputs())
        pair = tuple(replace(c, payload={**c.payload, 'extensionInsights': deepcopy(expected)}) for c in legacy)
        assert all(validate_product_payload(c.product, c.payload) == () for c in pair)
        def bound(candidates):
            checks = evaluate_macro_quality(CATALOG, candidates, (), extension_expected=expected, extension_required=True)
            return next(q for q in checks if q.name == 'macro-extensions-source-bound').passed
        assert bound(pair)
        assert not bound(legacy)
        changed = deepcopy(pair[1].payload)
        changed['extensionInsights']['fx']['value'] = 999
        assert 'extensionInsights' in validate_product_payload('macro-insights', changed)
        assert not bound((pair[0], replace(pair[1], payload=changed)))
        # 即使新内容自洽，也必须与加载的源证据相同，不能手工换成别的源。
        changed_inputs = inputs()
        changed_inputs['sources']['usdcnh']['reason'] = 'EXTENSION_PERMISSION_ERROR'
        other = build_extension_view(changed_inputs)
        assert not bound(tuple(replace(c, payload={**c.payload, 'extensionInsights': other}) for c in pair))
    finally:
        engine.dispose()
