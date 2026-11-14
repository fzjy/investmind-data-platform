from app.macro.routes import transmission_view


def test_initial_view_only_defers_scopes_without_mutating_published_payload():
    full = {'enterpriseAnalysis': {'inputHash': 'h', 'cards': [1], 'rea': {'x': 1},
        'industryComparison': {'groups': [1]}, 'officialTargets': {'targets': [2]}},
        'analysis': [3], 'seriesStatus': [4]}
    initial = transmission_view(full, 'initial')
    assert 'industryComparison' not in initial['enterpriseAnalysis']
    assert 'officialTargets' not in initial['enterpriseAnalysis']
    assert initial['enterpriseAnalysis']['cards'] == [1]
    assert initial['seriesStatus'] == [4]
    assert 'officialTargets' in full['enterpriseAnalysis']
    assert transmission_view(full, 'full') == full


def test_missing_enterprise_stays_missing():
    assert transmission_view({'analysis': []}, 'initial') == {'analysis': []}


def test_initial_route_preserves_metadata_and_passes_pinned_version():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.macro.routes import create_router
    from app.shared.contracts import DataResult, DataMeta
    versions = []
    result = DataResult(data={'enterpriseAnalysis': {'cards': [], 'officialTargets': {'targets': [1]}}},
        meta=DataMeta(source='database', version='v1', asOf='2026-11-10', stale=True, staleReason='old'))
    class Service:
        def get_transmission(self, version=None):
            versions.append(version)
            return result
    app = FastAPI(); app.include_router(create_router(service=Service()))
    with TestClient(app) as client:
        initial = client.get('/api/data/macro-transmission?view=initial&version=v1').json()
        full = client.get('/api/data/macro-transmission?version=v1').json()
        assert initial['meta'] == full['meta']
        assert initial['meta']['staleReason'] == 'old'
        assert 'officialTargets' not in initial['data']['enterpriseAnalysis']
        assert full['data']['enterpriseAnalysis']['officialTargets'] == {'targets': [1]}
        assert versions == ['v1', 'v1']
        assert client.get('/api/data/macro-transmission?view=unknown').status_code == 422
