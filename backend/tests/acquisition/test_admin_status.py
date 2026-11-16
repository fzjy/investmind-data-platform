from datetime import UTC, datetime
from app.acquisition.routes import envelope
from app.acquisition.contracts import *
from pathlib import Path

def test_admin_keeps_excluded_and_uses_local_route():
    row = StatusItem(StateKey('stock-daily','','security','600519.SH'),'日行情',AuditContext(),
                     Evidence('unrecorded'),Availability('available'),Assessment('excluded','无可信凭证'))
    item = envelope(StatusPage('1.0',datetime.now(UTC),[row],None))['items'][0]
    assert item['audit']['status'] == 'excluded' and item['key']['subjectKey'] == '600519.SH'
    js = (Path(__file__).resolve().parents[3]/'backend/app/local_admin/static/catalog.js').read_text(encoding='utf-8')
    assert '/admin/api/data-status?' in js and '单独说明' in js
