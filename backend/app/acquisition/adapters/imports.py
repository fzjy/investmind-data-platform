from app.acquisition.adapters.source_ids import public_source
from app.acquisition.contracts import StateKey
from app.acquisition.tracking import record_terminal


def import_keys(entry, source, detail=None):
    source = public_source(source)
    yield StateKey(entry,'','source',source), source
    if detail and entry == 'external-content' and detail in ('news','article','announcement'):
        yield StateKey(entry,detail,'source',source), source
    if detail and entry == 'external-macro':
        yield StateKey(entry,'series','series',public_source(detail)), source


def record_import(session, sources, file_row, now):
    for entry in sorted(set(sources)):
        for key, source in import_keys(*entry):
            record_terminal(session,key,started_at=now,checked_at=now,status='success',
                scope={'source':source,'fileHash':file_row.file_hash},
                references=({'fileId':file_row.id},),reason='文件校验通过，记录已接受')
