from app.acquisition.contracts import StateKey,StatusQuery
from app.acquisition.service import StatusService
from app.acquisition.adapters.targets import target_status
from backend.tests.macro.test_target_evidence_normalize import CAPTURED,AS_OF
from backend.tests.macro.test_target_evidence_policy import policy
from backend.tests.macro.test_target_evidence_store import seed


def test_etf_candidate_uses_its_own_observation(factory):
    from app.macro.target_evidence_store import TargetEvidenceStore
    store = TargetEvidenceStore(factory)
    oid=seed(store)
    store.normalize(oid,policy(),AS_OF,'Asia/Shanghai',persist=True)
    with factory() as session:
        proof,value=target_status(session,StateKey('etf-basic','','instrument','512880.SH'),CAPTURED)
        assert proof.status=='success' and value.kind=='available'
        assert proof.references==({'observationId':oid},)


def test_configured_targets_visible_without_synthesizing_success(factory):
    for entry in ('index-basic','index-weights','etf-basic'):
        page=StatusService(factory).query(StatusQuery((entry,),limit=2))
        assert len(page.items)==2
        assert all(r.evidence.status=='unrecorded' and r.assessment.status=='missing' for r in page.items)
