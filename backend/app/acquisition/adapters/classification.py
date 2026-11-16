from datetime import UTC, datetime

from app.acquisition.contracts import StateKey
from app.acquisition.tracking import record_terminal


def record_classification(session,identity,subject,*,member,error=None,empty=False,references=()):
    key = (StateKey('stock-classification-members','','security',subject) if member else
           StateKey('stock-classification','','industry',subject))
    now = datetime.now(UTC)
    record_terminal(session,key,started_at=now,checked_at=now,status='failed' if error else 'success',
                    code=error,scope={'source':'tushare','plan':identity,'confirmedEmpty':empty},
                    references=references)
