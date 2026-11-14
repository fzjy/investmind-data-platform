"""逐证券核对选股旧同比参考、支付率浮层、持有人序列和分类出处。"""
from argparse import ArgumentParser
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from math import isclose
from pathlib import Path
import json
import runpy
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"backend"))
from sqlalchemy import select, text
from app.config import settings
from app.db.models import (PublishedSnapshot, MetricValue, MetricInput, IncomeStatement, CashflowStatement,
                           TopHolder, DailyBasic, Security, Classification, ClassificationMember, FinancialIndicator,
                           IngestionBatch, SourcePayload)
from app.ingestion.repositories import content_hash
from app.shared.readonly_database import open_readonly_factory
from app.shared.stock_mapping import StockMappingPolicy
from app.shared.boards import normalize_board
from app.stocks.shareholder_counts import load_shareholder_counts

holding_period = runpy.run_path(str(Path(__file__).with_name("audit-screener-capital-fields.py")))["holding_period"]
GROWTH = {"revenueGrowth":("revenue","previous_revenue",("total_revenue","revenue")),
          "profitGrowth":("profit","previous_profit",("n_income_attr_p","n_income","total_profit")),
          "cashflowGrowth":("cfo","previous_cfo",("n_cashflow_act",))}


def same(a,b):
    if a is None or b is None:
        return a is b
    return isclose(float(a),float(b),rel_tol=1e-9,abs_tol=1e-7)


def group(rows,key):
    result=defaultdict(list)
    for row in rows:
        result[getattr(row,key)].append(row)
    return result


def source_key(row):
    return ":".join([row.ts_code,str(row.end_date),row.report_type or "",
                     str(row.ann_date) if row.ann_date else "",row.update_flag or ""])


def growth(metric, inputs, sources):
    current, previous, aliases = GROWTH[metric.metric_id]
    evidence = {}
    for part in (current,previous):
        refs = [r for r in inputs if r.input_name==part]
        assert len(refs)<=1, (metric.id,part)
        if not refs:
            evidence[part] = {"value":None, "coreIds":[], "reason":"未存该输入引用"}
            continue
        ref = refs[0]
        rows = sources.get((ref.source_table,ref.source_record_key),[])
        known = set()
        for row in rows:
            known.add(next(((key,Decimal(str(row.data[key]))) for key in aliases if row.data.get(key) is not None),(None,None)))
        assert len(known)<=1, (ref.id,known)
        field,v = next(iter(known),(None,None))
        evidence[part] = {"value":str(v) if v is not None else None, "coreIds":[r.id for r in rows],
                          "recordKey":ref.source_record_key, "sourceTable":ref.source_table, "sourceField":field}
    a,b=[evidence[p]["value"] for p in (current,previous)]
    value=None if a is None or b is None or Decimal(b)==0 else (
        (Decimal(a)-Decimal(b))/abs(Decimal(b))*100).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return value,evidence


def industry(nodes, members, policy):
    labels = defaultdict(set)
    by_code = {n.classification_id:n for n in nodes.values()}
    ids = set()
    valid=True
    for member in members:
        node=nodes.get(member.classification_id)
        seen=set()
        while node:
            if node.id in seen:
                valid=False
                break
            seen.add(node.id)
            ids.add(node.id)
            if node.name and node.name.strip():
                labels[node.level].add(node.classification_id)
            if node.level==1 and node.parent_id in ("0","",None):
                break
            parent=by_code.get(node.parent_id)
            if parent is None or parent.level!=node.level-1:
                valid=False
                break
            node=parent
    valid = valid and all(len(values)==1 for values in labels.values())
    def name(level):
        return by_code[next(iter(labels[level]))].name if valid and labels[level] else ""
    return name(policy.majorLevel),name(policy.subLevel),sorted(ids)


def inspect(session):
    snap=session.get(PublishedSnapshot,"screener")
    end=snap.as_of.date()
    stocks={r["symbol"]:r for r in snap.payload["stocks"]}
    codes=list(stocks)
    policy=StockMappingPolicy.load()
    sources=defaultdict(list)
    for table,model in (("core.income_statement",IncomeStatement),("core.cashflow_statement",CashflowStatement)):
        for r in session.scalars(select(model).where(model.ts_code.in_(codes),model.end_date<=end)):
            sources[(table,source_key(r))].append(r)
    metrics=list(session.scalars(select(MetricValue).where(MetricValue.ts_code.in_(codes),MetricValue.as_of==end,
                  MetricValue.metric_id.in_(GROWTH),MetricValue.formula_version=="1.0.0")))
    metric_by_key={(r.ts_code,r.metric_id):r for r in metrics}
    inputs=group(session.scalars(select(MetricInput).where(MetricInput.metric_value_id.in_([r.id for r in metrics]))),"metric_value_id")
    basics=group(session.scalars(select(DailyBasic).where(DailyBasic.ts_code.in_(codes),DailyBasic.trade_date<=end)),"ts_code")
    holders=group(session.scalars(select(TopHolder).where(TopHolder.ts_code.in_(codes),TopHolder.end_date<=end,TopHolder.ann_date<=end)),"ts_code")
    counts=group(load_shareholder_counts(session,codes,end),"ts_code")
    raw_counts=defaultdict(list)
    originals=session.scalars(select(SourcePayload).join(IngestionBatch).where(SourcePayload.api_name=="stk_holdernumber",
        IngestionBatch.source=="tushare",IngestionBatch.dataset=="shareholder_count",IngestionBatch.status=="completed",
        IngestionBatch.rejected_count==0).order_by(SourcePayload.id))
    for source in originals:
        if content_hash(source.payload)!=source.content_hash:
            continue
        for index,item in enumerate(source.payload.get("items",[])):
            raw=dict(zip(source.payload["fields"],item))
            if raw.get("ts_code") not in stocks or not all(raw.get(k) for k in ("ann_date","end_date")):
                continue
            holder_num=Decimal(str(raw["holder_num"])) if raw.get("holder_num") is not None else None
            try:
                k=(raw["ts_code"],date.fromisoformat(str(raw["end_date"])),date.fromisoformat(str(raw["ann_date"])),holder_num)
            except ValueError:
                continue  # 日期含时刻等非合同格式的原行没有参与正式发布。
            if k not in raw_counts:
                raw_counts[k]=[source.id,index]
    securities={r.ts_code:r for r in session.scalars(select(Security).where(Security.ts_code.in_(codes)))}
    nodes={r.id:r for r in session.scalars(select(Classification).where(Classification.provider==policy.classification.provider,
                                                                         Classification.source==policy.classification.source))}
    members=group(session.scalars(select(ClassificationMember).where(ClassificationMember.ts_code.in_(codes),
        ClassificationMember.effective_from<=end,(ClassificationMember.effective_to.is_(None))|(ClassificationMember.effective_to>=end),
        ClassificationMember.classification_id.in_(nodes))),"ts_code")
    indicators=group(session.scalars(select(FinancialIndicator).where(FinancialIndicator.ts_code.in_(codes),
                                      FinancialIndicator.end_date<=end)),"ts_code")
    summary=defaultdict(Counter)
    mismatches=[]
    details={}
    def check(code,key,got,expected):
        ok=same(got,expected)
        summary[key]["bothNull" if ok and got is None else "equalValue" if ok else "different"]+=1
        if not ok:
            mismatches.append([code,key,str(got),str(expected)])
    for code,stock in sorted(stocks.items()):
        record={}
        for key in GROWTH:
            metric=metric_by_key[code,key]
            expected,refs=growth(metric,inputs[metric.id],sources)
            check(code,key,stock[key],expected)
            check(code,key+".derived",metric.value,expected)
            record[key]={"derivedId":metric.id,"expected":str(expected) if expected is not None else None,"inputs":refs}
        ratios=stock["listHistory"]["dividends"]
        period=sorted(ratios,key=lambda r:r["year"])[-1] if ratios else {}
        check(code,"payoutRootEqualsThreeYear",stock["payoutRatio"],stock["researchValues"].get("payoutRatio"))
        expected=(period["cashAmount"]/period["parentProfit"]*100 if period.get("complete") and
                  period.get("cashAmount") is not None and period.get("parentProfit") else None)
        check(code,"latestFiscalPayout",period.get("payoutRatio"),expected)
        record["payout"]={"threeYear":stock["payoutRatio"],"latestFiscal":period.get("year"),"annual":period.get("payoutRatio")}
        for name, source in (("institutions",holders[code]),("holders",counts[code])):
            periods=group(source,"end_date")
            history=[]
            for day in sorted(periods)[-8:]:
                rows=periods[day]
                if name=="institutions":
                    val, info = holding_period(rows,basics[code],policy)
                else:
                    ann=max(r.ann_date for r in rows)
                    latest=[r for r in rows if r.ann_date==ann]
                    values={Decimal(str(r.holder_num)) if r.holder_num is not None else None for r in latest}
                    val=next(iter(values)) if len(values)==1 else None
                    info={"period":str(day),"announced":str(ann),"coreIds":sorted(r.id for r in latest if hasattr(r,"id")),
                          "rawObservations":[{"value":str(v) if v is not None else None,
                                              "payloadIdAndRow":raw_counts.get((code,day,ann,v))} for v in sorted(values,key=str)]}
                    assert all(o["payloadIdAndRow"] for o in info["rawObservations"]) or info["coreIds"]
                history.append(info | {"value":float(val) if val is not None else None})
            published=stock["listHistory"][name]
            assert [r["period"] for r in history]==[r["period"] for r in published],(code,name)
            for actual,want in zip(published,history):
                check(code,name,actual["value"],want["value"])
            record[name]=history
        reports=group([r for r in indicators[code] if r.report_type in ("",None,"1") and
                       (r.end_date.month,r.end_date.day) in ((3,31),(6,30),(9,30),(12,31)) and
                       (not (r.f_ann_date or r.ann_date) or (r.f_ann_date or r.ann_date)<=end)],"end_date")
        returns=stock["listHistory"].get("returns",[])
        assert [r["period"] for r in returns]==[str(d) for d in sorted(reports)],code
        return_rows=[]
        for point in returns:
            day=date.fromisoformat(point["period"])
            candidates=reports[day]
            kept=[r for r in candidates if (r.f_ann_date or r.ann_date) is None or (r.f_ann_date or r.ann_date)>=day] or candidates
            valid=all((r.f_ann_date or r.ann_date) and (r.f_ann_date or r.ann_date)>=day for r in kept)
            if valid:
                latest=max(((r.f_ann_date or r.ann_date),r.update_flag or "") for r in kept)
                kept=[r for r in kept if ((r.f_ann_date or r.ann_date),r.update_flag or "")==latest]
            ref={"period":str(day),"ids":sorted(r.id for r in kept)}
            for key in ("roe","roic"):
                values={Decimal(str(r.data[key])) if r.data.get(key) is not None else None for r in kept}
                want=next(iter(values)) if valid and len(values)==1 else None
                check(code,"report."+key,point[key],want)
                ref[key]=float(want) if want is not None else None
            return_rows.append(ref)
        record["returns"]=return_rows
        security=securities[code]
        major,sub,ids=industry(nodes,members[code],policy.classification)
        sec=security.data or {}
        expected={"industry":major,"majorIndustry":major,"subIndustry":sub,"region":sec.get("region") or sec.get("area") or "",
                  "board":normalize_board(security.board if security.board is not None else sec.get("board"),exchange=security.exchange,symbol=code),
                  "concepts":sec["concepts"] if isinstance(sec.get("concepts"),list) else []}
        for key,v in expected.items():
            if stock[key]!=v:
                mismatches.append([code,key,stock[key],v])
            summary["category."+key]["equalValue" if stock[key]==v and bool(v) else "equalEmpty" if stock[key]==v else "different"]+=1
        raw_refs=[]
        for member in members[code]:
            for ref in (member.data or {}).get("evidence",[]):
                origin=session.get(SourcePayload,ref["payloadId"])
                assert origin.api_name=="index_member_all" and content_hash(origin.payload)==origin.content_hash
                fields=origin.payload["fields"]
                raw=dict(zip(fields,origin.payload["items"][ref["rowNumber"]-1]))
                assert raw["ts_code"]==code and raw["l1_name"]==major and raw["l3_name"]==sub
                assert date.fromisoformat(raw["in_date"])==member.effective_from
                raw_refs.append([origin.id,ref["rowNumber"]-1])
        region_ref=sec.get("_regionLineage",{}).get("sourcePayloadId")
        if region_ref:
            origin=session.get(SourcePayload,region_ref)
            assert origin.api_name=="stock_basic" and content_hash(origin.payload)==origin.content_hash
            assert any(raw.get("ts_code")==code and raw.get("area")==expected["region"]
                       for raw in (dict(zip(origin.payload["fields"],r)) for r in origin.payload["items"]))
        record["classifications"]={"values":expected,"securityKey":code,"classificationNodeIds":ids,
                                   "membershipIds":[r.id for r in members[code]],"memberRawRows":raw_refs,
                                   "regionRawPayload":region_ref,"securityBoardRaw":security.board}
        details[code]=record
    return dict(asOf=str(end),version=snap.version,summary=summary,bySymbol=details,differences=mismatches)


if __name__=="__main__":
    parser=ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True,type=Path)
    args=parser.parse_args()
    with open_readonly_factory(settings.database_read_url) as factory,factory() as session:
        assert session.scalar(text("show transaction_read_only"))=="on"
        result=inspect(session)
    args.output.write_text(json.dumps(result,ensure_ascii=False,separators=(",",":"),allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps(result["summary"],ensure_ascii=True))
    if result["differences"]:
        print(json.dumps(result["differences"][:10],ensure_ascii=True))
        raise SystemExit(1)
