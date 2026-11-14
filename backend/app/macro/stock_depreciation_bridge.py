"""已验证cashflow原件补充三项折旧；只绑定唯一同版见证，不改原财报。"""
from collections import Counter
from dataclasses import asdict,replace
from datetime import date

from app.connectors.tushare.strategies import LEGACY_CASHFLOW_FIELDS
from app.macro.enterprise import ReaFieldInput,_hash,_number
from app.macro.receivables_match import _date_value,_same_number
from app.macro.renewal_estimation import FIELDS

META={'ts_code','end_date','ann_date','f_ann_date','report_type','comp_type','update_flag'}
WITNESS=set(LEGACY_CASHFLOW_FIELDS)-META


def bridge_depreciation(inputs,rows,*,available_date:date,contract_hash:str):
    """rows为上层已复核完整Raw链的(identity,raw_hash,original)；不读DB/网络。"""
    evidence=list(inputs.rea_fields);occupied={(f.source_key,f.field) for f in evidence}
    counts=Counter();matched=0
    for source in inputs.financials:
        if source.table!='cashflow' or source.comp_type!='1' or source.report_type!='1' or source.symbol not in inputs.symbols:
            continue
        missing=[f for f in FIELDS if f not in source.values and (source.source_key,f) not in occupied]
        if not missing:continue
        if not WITNESS<=set(source.values):counts['source-witness-missing']+=1;continue
        metadata=dict(ts_code=source.symbol,end_date=source.end_date.isoformat(),
            ann_date=source.ann_date.isoformat() if source.ann_date else None,
            f_ann_date=source.f_ann_date.isoformat() if source.f_ann_date else None,
            report_type=source.report_type,comp_type=source.comp_type,update_flag=source.update_flag)
        def same_meta(row):
            if not META<=set(row):return False
            for key,value in metadata.items():
                actual=row[key]
                if key in ('end_date','ann_date','f_ann_date'):actual=_date_value(actual)
                elif isinstance(actual,bool):return False
                else:actual=str(actual) if actual is not None else None
                if actual!=value:return False
            return True
        candidates=[r for r in rows if same_meta(r[2]) and WITNESS<=set(r[2])
            and all(_same_number(source.values[k],r[2][k]) for k in WITNESS)]
        if len(candidates)!=1:
            counts['ambiguous' if candidates else 'no-match']+=1;continue
        raw_id,raw_hash,row=candidates[0];source_hash=_hash(asdict(source))
        request_key='stock-depreciation:'+_hash(dict(contract=contract_hash,source=source_hash,raw=raw_id,rawHash=raw_hash))
        for field in missing:
            value=row.get(field);number=_number(value)
            if field not in row:status,value='missing-field',None
            elif value is None:status,value='returned-null',None
            elif number is None or number<0:status,value='invalid-value',None
            else:status,value='matched',format(number,'f');matched+=1
            digest=_hash(dict(requestKey=request_key,field=field,status=status,value=value,availableDate=available_date.isoformat()))
            evidence.append(ReaFieldInput(source.source_key,source_hash,field,status,value,digest,available_date,request_key))
            occupied.add((source.source_key,field));counts[status]+=1
    return (replace(inputs,rea_fields=tuple(evidence)) if len(evidence)!=len(inputs.rea_fields) else inputs),dict(
        matchedFields=matched,addedFields=len(evidence)-len(inputs.rea_fields),decisions=dict(counts))
