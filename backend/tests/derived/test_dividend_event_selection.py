from datetime import date
from decimal import Decimal

from sqlalchemy import select

from app.db.models import Dividend
from app.stocks.derived.inputs import CoreSnapshotRepository
from backend.tests.jobs.test_build_and_publish_integration import _factory
from backend.tests.marts.test_content_runtime import prepare


def setup():
    factory=_factory();prepare(factory)
    with factory.begin() as s:
        for r in s.scalars(select(Dividend)):s.delete(r)
    return factory


def add(factory,ann,ex,stage='实施',amount='1',period=date(2025,12,31)):
    with factory.begin() as s:
        s.add(Dividend(ts_code='600519.SH',end_date=period,ann_date=ann,div_proc=stage,
            cash_div=Decimal(amount),ex_date=ex,data={}))


def events(factory):
    return [a for a in CoreSnapshotRepository(factory).load(date(2026,8,24),('600519.SH',)).corporate_actions if isinstance(a,Dividend)]


def test_two_payments_same_fiscal_year_are_not_annual_deduplicated():
    f=setup();add(f,date(2026,3,1),date(2026,4,1));add(f,date(2026,5,1),date(2026,6,1))
    assert len(events(f))==2


def test_stopped_and_unrecognized_implementation_substrings_are_not_paid():
    f=setup();add(f,date(2026,3,1),date(2026,4,1));add(f,date(2026,3,2),date(2026,4,1),'停止实施')
    assert events(f)==[]


def test_same_date_conflict_cannot_select_arbitrary_implementation_variant():
    f=setup();add(f,date(2026,3,1),date(2026,4,1));add(f,date(2026,3,1),date(2026,4,1),'实施完成','2')
    assert events(f)==[]


def test_ambiguous_stop_without_event_dates_blocks_only_its_fiscal_year():
    f=setup();add(f,date(2026,3,1),date(2026,4,1));add(f,date(2026,3,2),None,'停止实施')
    add(f,date(2026,4,1),date(2026,5,1),period=date(2024,12,31))
    assert [r.end_date for r in events(f)]==[date(2024,12,31)]


def test_payment_date_revision_is_one_event_and_later_revision_wins():
    f=setup()
    with f.begin() as s:
        for ann,pay,value in [(date(2026,3,1),date(2026,4,2),'1'),(date(2026,3,2),date(2026,4,3),'1.2')]:
            s.add(Dividend(ts_code='600519.SH',end_date=date(2025,12,31),ann_date=ann,div_proc='实施',
                cash_div=Decimal(value),record_date=date(2026,3,31),ex_date=date(2026,4,1),pay_date=pay,data={}))
    selected=events(f)
    assert len(selected)==1 and selected[0].cash_div==Decimal('1.2')


def test_registration_date_added_on_revision_keeps_one_event():
    f=setup();add(f,date(2026,3,1),date(2026,4,1))
    with f.begin() as s:
        s.add(Dividend(ts_code='600519.SH',end_date=date(2025,12,31),ann_date=date(2026,3,2),div_proc='实施',
            cash_div=Decimal('1.2'),record_date=date(2026,3,31),ex_date=date(2026,4,1),data={}))
    result=events(f)
    assert len(result)==1 and result[0].cash_div==Decimal('1.2')


def test_future_implementation_revision_does_not_hide_known_old_record():
    f=setup();add(f,date(2026,3,1),date(2026,4,1))
    with f.begin() as s:
        s.add(Dividend(ts_code='600519.SH',end_date=date(2025,12,31),ann_date=date(2026,3,2),div_proc='实施',
            cash_div=Decimal('1.2'),ex_date=date(2026,4,1),data={'imp_ann_date':'20261101'}))
    result=events(f)
    assert len(result)==1 and result[0].cash_div==Decimal('1')


def test_registration_alone_is_not_a_paid_cash_event():
    f=setup()
    with f.begin() as s:
        s.add(Dividend(ts_code='600519.SH',end_date=date(2025,12,31),ann_date=date(2026,3,1),div_proc='实施',
            cash_div=Decimal('1'),record_date=date(2026,3,31),ex_date=None,pay_date=None,data={}))
    assert events(f)==[]
