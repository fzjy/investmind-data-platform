from datetime import UTC, datetime
from types import SimpleNamespace

from app.acquisition.adapters.content import record_news
from app.acquisition.adapters.imports import record_import
from app.acquisition.adapters.source_ids import public_source
from app.acquisition.availability.published import _item
from app.acquisition.contracts import StateKey, StatusQuery
from app.acquisition.repository import StateRepository
from app.acquisition.service import StatusService


def test_news_delivery_error_wins_same_poll(factory):
    now = datetime.now(UTC)
    poll = {'lastAttemptAt':now.isoformat(),'lastSuccessAt':now.isoformat()}
    with factory.begin() as session:
        record_news(session,poll,now)
        record_news(session,poll,now,'PUBLICATION_FAILED')
        key = StateKey('news','','source','sina-finance')
        assert StateRepository(session).read((key,))[key].error_code == 'PUBLICATION_FAILED'


def test_all_entries_for_one_security_can_page(factory):
    page = StatusService(factory).query(StatusQuery(subject_kind='security',subject_keys=('600519.SH',),limit=2))
    assert len(page.items)==2 and page.next_cursor


def test_announcement_empty_depends_on_covered_window():
    key = StateKey('stock-announcements','','security','600519.SH','historical')
    product = 'stock-workbench:600519.SH'
    window = {'start':'2026-01-01','end':'2026-01-31','status':'completed','count':0}
    coverage = {'start':window['start'],'end':window['end'],'windows':[window]}
    payload = {'dailyChart':{'announcements':[],'announcementCoverage':coverage}}
    assert _item(key,product,payload,'2026-01-13').kind=='confirmed_empty'
    assert _item(key,product,payload,'2026-02-10').kind=='unknown'
    window['status']='partial'
    assert _item(key,product,payload,None).kind=='unknown'


def test_member_rows_alone_cannot_prove_enterprise_result():
    key = StateKey('macro-enterprise','000300.SH','product','macro-transmission')
    target = {'code':'000300.SH','status':'available','poolMembers':['600519.SH'],'analysis':{}}
    payload = {'enterpriseAnalysis':{'officialTargets':{'targets':[target]}}}
    assert _item(key,'macro-transmission',payload,None).kind=='missing'
    target['status']='no-intersection'
    assert _item(key,'macro-transmission',payload,None).kind=='not_applicable'


def test_import_source_and_content_subtype(factory):
    now = datetime.now(UTC)
    with factory.begin() as session:
        record_import(session,[('external-content','https://example.cn','article'),
            ('external-content','公开来源','news')],SimpleNamespace(id=7,file_hash='verified'),now)
        known = StateKey('external-content','article','source',public_source('https://example.cn'))
        other = StateKey('external-content','news','source',known.subject_key)
        result = StateRepository(session).read((known,other))
        assert result[known].status=='success' and other not in result
    page = StatusService(factory).query(StatusQuery(('external-content',),('news',),subject_keys=('公开来源',)))
    assert page.items[0].evidence.status=='success'
