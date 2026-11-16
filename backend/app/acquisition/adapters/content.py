from datetime import datetime
from app.acquisition.contracts import StateKey
from app.acquisition.tracking import record_terminal


def record_news(session, state, now, delivery_error=None):
    checked = state.get('lastAttemptAt')
    if not checked and not delivery_error:
        return
    checked = now if delivery_error else datetime.fromisoformat(checked)
    if checked.utcoffset() is None or checked > now:
        return
    error = delivery_error or state.get('lastError')
    if error == 'SOURCE_STALE':
        error = None
    status = 'failed' if error else 'unrecorded' if state.get('nextId') or state.get('lastSuccessAt') != state.get('lastAttemptAt') else 'success'
    record_terminal(session, StateKey('news','','source','sina-finance'), started_at=checked,
                    checked_at=checked, status=status, code=error,
                    attempt_id='news-'+checked.isoformat()+('-2' if delivery_error else '-1'), scope={'source':'sina-finance'},
                    reason={'success':'来源检查完成','unrecorded':'分页尚未完成','failed':'新闻采集或交付失败'}[status])
