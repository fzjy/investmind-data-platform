from datetime import date
from decimal import Decimal
from app.stocks.derived.inputs import FinancialPoint

AS_OF=date(2026,10,21)


def point(period='2025-12-31',visible='2026-04-01',source='core.financial_indicator',report=None,**values):
    return FinancialPoint(date.fromisoformat(period),date.fromisoformat(visible) if visible else None,
        report,values,'600519.SH',source,'source-'+str(values))


def project(*points):
    from app.stocks.return_history import return_history
    return return_history(points,AS_OF)


def test_indicator_percentages_not_rescaled_or_substituted_with_weighted_or_annualized_fields():
    result=project(point(roe=20,roic=12,roe_waa=99,roe_yearly=80))
    row=result['reports'][0]
    assert row['roe']==20 and row['roic']==12 and row['premium']==8
    assert result['asOf']=='2026-10-21'
    assert row['period']=='2025-12-31' and row['frequency']=='FY'
    assert row['announcedAt']=='2026-04-01'


def test_latest_visible_revision_keeps_missing_fields_missing_and_future_revision_out():
    rows=[point(roe=10,roic=8),point(visible='2026-05-01',roe=20,roic=None),point(visible='2026-11-01',roe=30,roic=20)]
    row=project(*rows)['reports'][0]
    assert row['roe']==20 and row['roic'] is None and row['premium'] is None


def test_same_version_conflict_is_field_local_order_independent_and_update_flag_wins():
    a=point(roe=20,roic=12);b=point(roe=21,roic=12)
    row=project(a,b)['reports'][0]
    assert row['roe'] is None and row['roic']==12
    assert '同版本冲突' in row['reasons']['roe']
    assert project(a,b)==project(b,a)
    row=project(a,point(roe=25,roic=15,update_flag='1'))['reports'][0]
    assert row['roe']==25 and row['roic']==15


def test_zero_negative_and_nonfinite_are_not_missing_or_safe_by_default():
    zero=project(point(roe=0,roic=-2))['reports'][0]
    assert zero['roe']==0 and zero['premium']==2
    bad=project(point(roe=False,roic=float('nan')))['reports'][0]
    assert bad['roe'] is None and bad['roic'] is None


def test_financial_pk_prefers_unambiguous_fields_of_the_same_visible_version():
    row=project(point(interestdebt=100,netprofit_margin=10,quick_ratio=0,profit_dedt=-20),
                point(interestdebt=100,netprofit_margin=11,quick_ratio=0,profit_dedt=-20),
                point(visible='2026-11-01',grossprofit_margin=12))['reports'][0]
    assert row['financialPk']==dict(interestdebt=100,quick_ratio=0,profit_dedt=-20)


def test_non_indicator_future_and_non_cumulative_parent_reports_do_not_enter_history():
    result=project(point(source='core.income_statement',roe=99,roic=99),
        point(period='2026-12-31',roe=30),point(report='2',roe=50),point(report='4',roe=60))
    assert result['reports']==[]


def test_unknown_disclosure_and_invalid_order_are_explicitly_missing_not_report_date_proxy():
    for p in [point(visible=None,roe=20,roic=12),point(visible='2025-01-01',roe=20,roic=12)]:
        row=project(p)['reports'][0]
        assert row['roe'] is None and row['announcedAt'] is None
        assert '披露' in row['reasons']['roe']


def test_old_invalid_disclosure_cannot_erase_latest_valid_record():
    row=project(point(visible='2025-01-01',roe=99,roic=99),point(roe=20,roic=12))['reports'][0]
    assert row['roe']==20 and row['roic']==12 and row['announcedAt']=='2026-04-01'


def test_real_repository_omitted_missing_disclosure_reaches_workbench_as_diagnostic():
    from app.db.models import FinancialIndicator
    from backend.tests.marts.test_content_runtime import prepare
    from backend.tests.jobs.test_build_and_publish_integration import _factory
    from app.stocks.derived.inputs import CoreSnapshotRepository
    from app.stocks.marts.stocks import prepare_workbench_inputs
    f=_factory();prepare(f)
    with f.begin() as s:
        s.add(FinancialIndicator(ts_code='600519.SH',end_date=date(2024,12,31),ann_date=None,data={'roe':99,'roic':88}))
    snapshot=CoreSnapshotRepository(f).load(AS_OF,('600519.SH',))
    assert not any(p.end_date==date(2024,12,31) and p.source_table=='core.financial_indicator' for p in snapshot.financial_points)
    result=prepare_workbench_inputs(snapshot,f).return_histories['600519.SH']
    row=next(r for r in result['reports'] if r['period']=='2024-12-31')
    assert row['roe'] is None and row['roic'] is None
    assert row['announcedAt'] is None and '披露' in row['reasons']['roe']


def test_quarter_reports_remain_cumulative_and_decimal_duplicates_do_not_conflict():
    result=project(point(period='2026-06-30',visible='2026-08-01',roe=Decimal('8.00'),roic=Decimal('5.0')),
        point(period='2026-06-30',visible='2026-08-01',roe='8',roic=5))
    assert len(result['reports'])==1
    assert result['reports'][0]['frequency']=='YTD'
    assert result['reports'][0]['roe']==8


def test_builder_uses_raw_versions_for_return_history_before_shared_dedup():
    from dataclasses import replace
    from backend.tests.marts.test_content_runtime import prepare
    from backend.tests.jobs.test_build_and_publish_integration import _factory
    from app.stocks.derived.inputs import CoreSnapshotRepository
    from app.stocks.marts.stocks import prepare_workbench_inputs,StockWorkbenchMartBuilder
    f=_factory();prepare(f)
    snapshot=CoreSnapshotRepository(f).load(AS_OF,('600519.SH',))
    snapshot=replace(snapshot,financial_points=(*snapshot.financial_points,point(roe=20,roic=12),point(roe=21,roic=12)))
    prepared=prepare_workbench_inputs(snapshot,f)
    result=StockWorkbenchMartBuilder(f).build(snapshot,'600519.SH','test',prepared_inputs=prepared)
    row=result.payload['financial']['dashboard']['returnHistory']['reports'][0]
    assert row['roe'] is None and row['roic']==12
