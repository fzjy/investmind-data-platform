from datetime import date
from hashlib import sha256
import json
import pytest
from app.ingestion.repositories import content_hash

def setup(tmp_path):
    from backend.tests.support.manual_dividend_resolution import resolve_announced_supersession
    old=dict(ts_code='600001.SH',end_date='20241231',ann_date='20250325',div_proc='停止实施',record_date=None,ex_date=None,sourceId='raw:old')
    new=dict(ts_code='600001.SH',end_date='20241231',ann_date='20250607',div_proc='实施',record_date='20250728',ex_date='20250729',sourceId='raw:new')
    (tmp_path/'doc.pdf').write_bytes(b'%PDF-evidence')
    fingerprint=lambda r:content_hash({k:v for k,v in r.items() if k!='sourceId'})
    rule=dict(sourceHash='bound',cancelHash=fingerprint(old),replacementHash=fingerprint(new),scope='proposal-only',documents=[dict(file='doc.pdf',sha256=sha256(b'%PDF-evidence').hexdigest(),url='https://www.hkexnews.hk/a.pdf',publishedAt='2025-07-21')])
    manifest=tmp_path/'rules.json';manifest.write_text(json.dumps([rule]),encoding='utf-8')
    return resolve_announced_supersession,[old,new],manifest

def test_verified_resolution_only_removes_bound_cancel_and_keeps_input(tmp_path):
    apply,rows,path=setup(tmp_path)
    result,proofs=apply(rows,'bound',date(2026,10,21),path)
    assert result==[rows[1]] and len(rows)==2
    assert proofs['raw:new'][0]['url']=='https://www.hkexnews.hk/a.pdf'

def test_future_proof_and_different_source_do_not_clear_gap(tmp_path):
    apply,rows,path=setup(tmp_path)
    assert apply(rows,'bound',date(2025,7,20),path)==(rows,{})
    assert apply(rows,'other',date(2026,10,21),path)==(rows,{})

def test_bad_original_fails_closed_and_mismatched_row_is_untouched(tmp_path):
    apply,rows,path=setup(tmp_path)
    assert apply([rows[0]|{'ann_date':'20250326'},rows[1]],'bound',date(2026,10,21),path)[1]=={}
    (tmp_path/'doc.pdf').write_bytes(b'bad')
    with pytest.raises(ValueError):apply(rows,'bound',date(2026,10,21),path)
