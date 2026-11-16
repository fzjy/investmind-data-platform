from app.acquisition.contracts import StateKey
from app.acquisition.tracking import record_terminal


def record_import(session, sources, file_row, now):
    for entry, source in sorted(set(sources)):
        record_terminal(session, StateKey(entry,'','source',source), started_at=now, checked_at=now,
                        status='success', scope={'source':source,'fileHash':file_row.file_hash},
                        references=({'fileId':file_row.id},), reason='文件校验通过，记录已接受')
