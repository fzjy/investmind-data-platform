"""受控财报见证和供应商协议样本，不读用户配置。"""
from dataclasses import replace
from datetime import date

from app.connectors.tushare.contracts import TushareResponse
from app.macro.enterprise import EnterprisePolicy
from app.macro.receivables_preview import preview_receivables
from backend.tests.macro.test_enterprise import inputs

AS_OF=date(2024,5,31)
POLICY=EnterprisePolicy(min_samples=1,min_coverage=50)


def full_inputs():
    data=inputs()
    witness={'total_assets':1000,'total_liab':500,'total_hldr_eqy_exc_min_int':500,
        'money_cap':100,'goodwill':None,'st_borr':0,'lt_borr':20,'st_bonds_payable':None,
        'non_cur_liab_due_1y':0,'bond_payable':None,'lease_liab':0,'total_share':100}
    return replace(data,financials=tuple(replace(r,values={**witness,**r.values})
        if r.table=='balance' else r for r in data.financials))


def request_and_source(data=None):
    data=data if data is not None else full_inputs()
    request=preview_receivables(data,AS_OF,POLICY,8,100)['requests'][0]
    source=next(r for r in data.financials if r.source_key==request['coreBinding']['sourceKey'])
    return request,source


def response_for(source,value=123):
    values={'ts_code':source.symbol,'ann_date':source.ann_date.strftime('%Y%m%d') if source.ann_date else None,
        'f_ann_date':source.f_ann_date.strftime('%Y%m%d') if source.f_ann_date else None,
        'end_date':source.end_date.strftime('%Y%m%d'),'report_type':source.report_type,
        'comp_type':source.comp_type,'update_flag':source.update_flag,**source.values,'accounts_receiv':value}
    return TushareResponse(fields=tuple(values),items=(tuple(values.values()),))


def alter_response(response,*,remove=None,change=None):
    rows=[dict(zip(response.fields,row,strict=True)) for row in response.items]
    for row in rows:
        for key in remove or (): row.pop(key,None)
        row.update(change or {})
    fields=tuple(rows[0]) if rows else response.fields
    return TushareResponse(fields=fields,items=tuple(tuple(row[key] for key in fields) for row in rows))
