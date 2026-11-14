"""规模变化不得清卡、丢候选或悄悄扩大默认开发范围。"""
from datetime import date
from dataclasses import replace
import json

import pytest
from typer.testing import CliRunner
from sqlalchemy import select

from app.cli.main import app
from app.config import Settings
from app.db.models import Security, Classification, ClassificationMember
from app.macro.enterprise import EnterpriseInputs, EnterprisePolicy, FinancialRecord, IndustryInput, SecurityInput
from app.macro.enterprise_contract import enterprise_loader, public_enterprise_analysis, valid_enterprise_analysis
from backend.tests.macro.test_enterprise_cli import database


def scaled_inputs(size):
    symbols=tuple(f'{i:06d}.SZ' for i in range(1,size+1))
    rows=[]
    for symbol in symbols:
        for year,revenue,profit,inventory,receivable in ((2023,100,10,100,100),(2024,110,5,120,150)):
            for table,values in (('income',{'revenue':revenue,'n_income_attr_p':profit}),
                                 ('balance',{'inventories':inventory,'accounts_receiv':receivable})):
                rows.append(FinancialRecord(table,symbol,date(year,3,31),date(year,4,30),None,'1','1','1',values,f'{table}:{symbol}:{year}'))
    return EnterpriseInputs('controlled-scale',symbols,
        tuple(SecurityInput(s,date(2020,1,1)) for s in symbols),tuple(rows),
        tuple(IndustryInput(s,'801120.SI',date(2020,1,1),None) for s in symbols),())


@pytest.mark.parametrize('size',[5,61,5000])
def test_three_card_contract_accepts_actual_sample_scale_without_fifty_cap(size):
    result=public_enterprise_analysis(scaled_inputs(size),date(2024,5,31),EnterprisePolicy(min_samples=1,max_quarters=1))
    assert result['poolCount']==size
    for card,values in zip(result['cards'],[(10,-50,60),(10,20,-10),(50,10,40)]):
        point=card['points'][-1]
        assert (point['primary'],point['secondary'],point['result'])==values
        assert (point['sampleCount'],point['candidateCount'],point['coverage'])==(size,size,100)
    assert valid_enterprise_analysis(result)


def test_policy_and_settings_allow_minimum_above_development_pool_size():
    settings=Settings(macro_enterprise_min_samples=51)
    policy=EnterprisePolicy(min_samples=settings.macro_enterprise_min_samples)
    result=public_enterprise_analysis(scaled_inputs(61),date(2024,5,31),policy)
    assert valid_enterprise_analysis(result)
    assert all(c['points'][-1]['sampleCount']==61 for c in result['cards'])


def test_financial_nonapplicable_and_missing_reports_have_distinct_dynamic_denominators():
    data=scaled_inputs(5)
    rows=tuple(replace(r,comp_type='2') if r.symbol=='000001.SZ' else r
        for r in data.financials if not (r.symbol=='000002.SZ' and r.table=='income' and r.end_date.year==2024))
    result=public_enterprise_analysis(replace(data,financials=rows),date(2024,5,31),EnterprisePolicy(min_samples=1,max_quarters=1))
    for c in result['cards']:
        q=c['quality'][-1]
        assert (q['sampleCount'],q['candidateCount'],q['coverage'],q['status'])==(3,4,75,'insufficient-coverage')
        assert q['nonApplicable']['000001.SZ']=='financial-report'
        assert q['excluded']['000002.SZ']=='missing-report'
    assert valid_enterprise_analysis(result)


@pytest.mark.parametrize('damage',['count','membership','unsafe-policy','unsafe-refs','bool','negative'])
def test_scale_does_not_relax_counts_membership_or_json_safe_integers(damage):
    result=public_enterprise_analysis(scaled_inputs(5),date(2024,5,31),EnterprisePolicy(min_samples=1))
    if damage=='count': result['cards'][0]['points'][-1]['sampleCount']=6
    if damage=='membership': result['cards'][0]['quality'][-1]['included'][0]='outside'
    if damage=='unsafe-policy': result['policy']['min_samples']=2**53
    if damage=='unsafe-refs': result['cards'][0]['quality'][-1]['sourceRefCount']=2**53
    if damage=='bool': result['poolCount']=True
    if damage=='negative': result['cards'][0]['quality'][-1]['candidateCount']=-1
    assert not valid_enterprise_analysis(result)


def configured_file(tmp_path,monkeypatch,symbols):
    path=tmp_path/'enterprise-symbols.json'
    path.write_text(json.dumps(symbols),encoding='utf-8')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_UNIVERSE','custom')
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_UNIVERSE_FILE',str(path))
    return path


def body(result):
    assert result.exit_code==0,result.stdout
    return json.loads(result.stdout.strip())


def test_custom_61_scope_is_shared_by_publication_audit_preview_and_dry_run(database,tmp_path,monkeypatch):
    path,factory=database
    configured_file(tmp_path,monkeypatch,[f'{i:06d}.SZ' for i in range(1,62)])
    before=path.read_bytes()
    result=enterprise_loader(factory,Settings.from_env())(date(2024,5,31))
    assert result['poolCount']==61
    q=result['cards'][0]['quality'][-1]
    assert (q['sampleCount'],q['candidateCount'])==(1,61)
    assert q['coverage']==pytest.approx(100/61)
    assert valid_enterprise_analysis(result)
    for command in (['audit-macro-enterprise'],['preview-enterprise-receivables'],['sync-enterprise-receivables','--dry-run']):
        output=body(CliRunner().invoke(app,command))
        assert output['poolCount']==61
        assert output.get('requestCount',0)==0 and output.get('physicalHttpExecuted',0)==0
    assert path.read_bytes()==before


def test_database_scope_reloads_members_without_excluding_missing_financials(database,monkeypatch):
    path,factory=database
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_UNIVERSE','all')
    with factory.begin() as session:
        session.get(Security,'000001.SZ').status='L'
        session.add(Security(ts_code='000002.SZ',symbol='000002',name='Missing report',status='L',list_date=date(2020,1,1)))
        session.add(Security(ts_code='000003.SZ',symbol='000003',name='Delisted',status='D',list_date=date(2020,1,1)))
        leaf=session.scalar(select(Classification.id).where(Classification.level==3))
        session.add(ClassificationMember(ts_code='000002.SZ',classification_id=leaf,effective_from=date(2020,1,1)))
    load=enterprise_loader(factory,Settings.from_env())
    before=path.read_bytes()
    first=load(date(2024,5,31))
    assert path.read_bytes()==before
    assert first['symbols']==['000001.SZ','000002.SZ']
    q=first['cards'][0]['quality'][-1]
    assert (q['sampleCount'],q['candidateCount'],q['coverage'])==(1,2,50)
    assert q['excluded']['000002.SZ']=='missing-report'
    with factory.begin() as session:
        session.add(Security(ts_code='000004.SZ',symbol='000004',name='New member',status='L',list_date=date(2020,1,1)))
    second=load(date(2024,5,31))
    assert second['poolCount']==3 and second['poolVersion']!=first['poolVersion']
    assert second['inputHash']!=first['inputHash']


def test_database_scope_reads_5000_real_members_without_write_or_financial_prefilter(database,monkeypatch):
    path,factory=database
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_UNIVERSE','all')
    with factory.begin() as session:
        session.get(Security,'000001.SZ').status='L'
        session.add_all(Security(ts_code=f'{i:06d}.SZ',symbol=f'{i:06d}',name='Controlled missing',status='L',list_date=date(2020,1,1)) for i in range(2,5001))
    before=path.read_bytes()
    result=enterprise_loader(factory,Settings.from_env())(date(2024,5,31))
    assert path.read_bytes()==before
    assert result['poolCount']==len(result['symbols'])==5000
    q=result['cards'][0]['quality'][-1]
    assert (q['sampleCount'],q['candidateCount'],q['coverage'],q['status'])==(1,5000,0.02,'insufficient-coverage')
    assert valid_enterprise_analysis(result)


@pytest.mark.parametrize('mode,file',[('custom',None),('all','unexpected.json'),('representative-50','unexpected.json')])
def test_invalid_scope_configuration_is_rejected_without_default_fallback(mode,file):
    with pytest.raises(ValueError): Settings(macro_enterprise_universe=mode,macro_enterprise_universe_file=file)


def test_empty_database_scope_and_invalid_custom_file_fail_without_fallback(database,tmp_path,monkeypatch):
    _,factory=database
    monkeypatch.setenv('DANG_MACRO_ENTERPRISE_UNIVERSE','all')
    with pytest.raises(ValueError): enterprise_loader(factory,Settings.from_env())(date(2024,5,31))
    configured_file(tmp_path,monkeypatch,['000001.SZ','000001.SZ'])
    with pytest.raises(ValueError): enterprise_loader(factory,Settings.from_env())(date(2024,5,31))


def test_json_unsafe_minimum_is_refused_in_settings_and_policy():
    with pytest.raises(ValueError): Settings(macro_enterprise_min_samples=2**53)
    with pytest.raises(ValueError): EnterprisePolicy(min_samples=2**53)
