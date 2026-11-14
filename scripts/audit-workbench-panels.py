"""按当前工作台指针核对八卡、估值带、股东回报和经营面板。"""
from argparse import ArgumentParser
from collections import Counter, defaultdict
from datetime import datetime
import json
from math import isclose
from pathlib import Path
from statistics import mean, pstdev
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sqlalchemy import select, text
from app.config import settings
from app.db.models import Classification, ClassificationMember, Security
from app.shared.readonly_database import open_readonly_factory
from app.shared.stock_mapping import classification_fields, StockMappingPolicy
from app.stocks.business_projection import load_business_histories
from app.stocks.derived.inputs import load_metric_card_inputs, CoreSnapshotRepository
from app.stocks.derived.workbench_cards import CARD_IDS, CARD_VERSION, calculate_metric_cards, display_card
from app.stocks.marts.shareholder_returns import json_numbers
from app.stocks.operating_history import operating_history
from app.stocks.return_history import return_history, disclosure_diagnostics
from app.stocks.shareholder_returns import load_shareholder_returns
from app.stocks.workbench_enrichment import load_holders


def comparable_points(points):
    return [(p['period'],p['value']) for p in points]


def audit(factory, session):
    assert session.scalar(text('show transaction_read_only')) == 'on'
    rows = session.execute(text("""
        select product, (as_of at time zone 'UTC')::date as day, version,
               payload->'quote' as quote, payload->'dataAvailability'->'intraday' as minute,
               payload->'metrics' as metrics, payload->'financial'->'dashboard' as dashboard,
               payload->'financial'->'statements'->'tables' as tables,
               payload->'financial'->'shareholders' as shareholders
        from mart.published_snapshot where product like 'stock-workbench:%' order by product
    """)).mappings().all()
    codes = [r['product'].split(':')[1] for r in rows]
    days = {r['day'] for r in rows}
    assert len(days)==1 and codes, '需按共同业务日分批'
    day = days.pop()
    inputs = load_metric_card_inputs(session, codes, day)
    dividends = load_shareholder_returns(session, codes, day, card_inputs=inputs)
    holdings = load_holders(factory, codes, day)
    businesses = load_business_histories(factory, codes, day)
    _, financial_points, _ = CoreSnapshotRepository(factory)._load_financial(session, tuple(codes), day)
    financial_points = (*financial_points,*disclosure_diagnostics(factory,codes,day))
    mention_rows = defaultdict(list)
    for s, ts in session.execute(text("""
        select symbol,published_at from core.content_document
        cross join lateral (select distinct json_array_elements_text(securities) as symbol) linked
        where copyright_status in ('licensed','public_domain','link_only') and symbol=any(:codes)
        and published_at<=current_timestamp order by symbol,published_at
    """),{'codes':codes}):
        mention_rows[s].append(ts)
    values = {(s,m):v for s,m,v in session.execute(text("""
        select ts_code,metric_id,value from derived.metric_value where ts_code=any(:codes)
        and metric_id=any(:metrics) and formula_version=:ver and as_of=:day
    """), {'codes':codes, 'metrics':list(CARD_IDS),'ver':CARD_VERSION,'day':day})}
    securities = {s.ts_code:s for s in session.scalars(select(Security).where(Security.ts_code.in_(codes)))}
    snapshot = SimpleNamespace(as_of=day, classifications=list(session.scalars(select(Classification))),
                               classification_members=list(session.scalars(select(ClassificationMember).where(ClassificationMember.ts_code.in_(codes)))))
    policy = StockMappingPolicy.load()
    prices = defaultdict(list)
    for row in session.execute(text("""
        select ts_code, trade_date,pe,pb,data from core.daily_basic
        where ts_code=any(:codes) and trade_date between (cast(:day as date)-interval '3 years') and :day
        order by ts_code,trade_date
    """),{'day':day,'codes':codes}).mappings():
        prices[row['ts_code']].append(row)
    counts, card_counts, per_symbol, errors = Counter(), defaultdict(Counter), {}, []
    def check(actual, expected, code, field):
        if actual != expected:
            errors.append([code,field])
    for r in rows:
        code = r['quote']['symbol']
        assert r['product'] == 'stock-workbench:'+code
        q, metrics, dash, shareholder = r['quote'], r['metrics'], r['dashboard'], r['shareholders']
        categories,_ = classification_fields(snapshot,securities[code],policy)
        for k in ('industry','subIndustry'):
            check(q[k],categories[k],code,k)
            counts[k+'Present'] += bool(q[k])
        counts['hasMentions'] += bool(q['latestMention'])
        # 内容重建沿用价格快照日期，内容有独立截止时点。
        # 按显示的最后提及核对能追到的来源数，并单列之后已入库的文档。
        latest_mention = datetime.fromisoformat(q['latestMention']) if q['latestMention'] else None
        visible_mentions = [ts for ts in mention_rows[code] if latest_mention and ts <= latest_mention]
        check(q['mentions'],len(visible_mentions),code,'quote.mentionsThroughDisplayedLatest')
        check(latest_mention,visible_mentions[-1] if visible_mentions else None,code,'quote.latestMention')
        counts['symbolsWithLaterContent'] += len(visible_mentions) < len(mention_rows[code])
        counts['mentionsAfterPriceDay'] += bool(latest_mention and latest_mention.date() > day)
        counts['conceptPopulated'] += bool(q['concepts'])
        counts['intradayAvailable'] += bool(r['minute']['available'])
        counts['intradayDayPopulated'] += bool(q['intraday']['day'])
        counts['intradayFiveDayPopulated'] += bool(q['intraday']['fiveDay'])
        card_detail={}
        shown={c['id']:c for c in metrics['cards'] if c.get('id') in CARD_IDS}
        check(sorted(shown),sorted(CARD_IDS),code,'metrics.cards')
        for card in calculate_metric_cards(inputs[code]):
            k=card.metric_id
            check(card.value,values[(code,k)],code,'derived.metric_value.'+k)
            check(shown.get(k),display_card(k,day.year,card.value,card.calculation()),code,'metrics.cards.'+k)
            card_counts[k]['value' if card.value is not None else 'missing'] += 1
            card_detail[k]={'value':str(card.value) if card.value is not None else None,
                            'missing':card.missing[:2], 'sourceCount':len(card.sources)}
        projected=json_numbers(dividends[code])
        for k,expected in projected.items():
            check(shareholder.get(k),expected,code,'shareholders.'+k)
        hold=holdings.get(code,[])
        check(shareholder.get('periods',[]),hold,code,'shareholders.periods')
        check(shareholder.get('holders',[]),hold[-1]['holders'] if hold else [],code,'shareholders.holders')
        for distribution in projected['annualDistributions']:
            counts['distribution.'+distribution['status']] += 1
        counts['concentrationPeriods'] += len(projected['concentrationPeriods'])
        counts['concentrationAverageKnown'] += sum(p['averageAmount'] is not None for p in projected['concentrationPeriods'])
        counts['holdingPeriods'] += len(hold)
        counts['holdingPeriodsWithRows'] += sum(bool(p['holders']) for p in hold)
        check(dash.get('businessHistory'),businesses.get(code),code,'dashboard.businessHistory')
        check(dash.get('operatingHistory'),operating_history(r['tables'],day),code,'dashboard.operatingHistory')
        expected_returns=return_history([p for p in financial_points if p.ts_code==code],day)
        check(dash.get('returnHistory'),expected_returns,code,'dashboard.returnHistory')
        counts['roeRoicPeriods'] += len(expected_returns['reports'])
        business=dash.get('businessHistory',{}).get('reports',[])
        counts['businessReports'] += len(business)
        counts['businessReportsQueryComplete'] += sum(bool(p['queryComplete']) for p in business)
        counts['operatingAnnualPeriods'] += len(dash.get('operatingHistory',{}).get('annual',[]))
        counts['operatingQuarterPeriods'] += len(dash.get('operatingHistory',{}).get('quarterly',[]))
        band_detail={}
        for band in dash['bands']:
            k=band['id']
            points=[]
            for basic in prices[code]:
                val=(basic['data'] or {}).get('ps_ttm',(basic['data'] or {}).get('ps')) if k=='ps' else basic[k]
                if val is not None and float(val)>0:
                    points.append((basic['trade_date'].isoformat(),float(val)))
            check(comparable_points(band['points']),points,code,'dashboard.bands.'+k+'.points')
            check(band['current'],points[-1][1] if points else None,code,'dashboard.bands.'+k+'.current')
            numbers=[v for _,v in points]
            bands=[mean(numbers)+a*pstdev(numbers) for a in (2,1,0,-1,-2)] if numbers else []
            check(len(bands)==len(band['bands']) and all(isclose(a,b,abs_tol=1e-8,rel_tol=1e-9)
                  for a,b in zip(bands,band['bands'])),True,code,'dashboard.bands.'+k+'.bands')
            band_detail[k]={'points':len(points),'first':points[0][0] if points else None,
                            'last':points[-1][0] if points else None,'current':band['current']}
            counts['bandPoints.'+k] += len(points)
        peg=dash['peg']
        counts['pegAvailable'] += peg['value'] is not None and all((peg[k] or 0)>0 for k in ('pe','growth'))
        counts['businessSymbols'] += bool(business)
        counts['returnHistorySymbols'] += bool(dash.get('returnHistory',{}).get('reports'))
        counts['ratioInputFields'] += len(metrics['fields'])
        latest = projected['annualDistributions'][-1] if projected['annualDistributions'] else {}
        per_symbol[code]={'version':r['version'],'industry':q['industry'],'subIndustry':q['subIndustry'],
            'mentions':{'published':q['mentions'],'latest':q['latestMention'],'throughLatest':len(visible_mentions),
                        'laterDocuments':len(mention_rows[code])-len(visible_mentions)},
            'cards':card_detail,'bands':band_detail,'businessReports':len(business),'latestHolders':len(shareholder['holders']),
            'latestDistribution':{k:latest.get(k) for k in ('year','status','dps','cashAmount','parentProfit','payoutRatio')},
            'ratioYears':len(metrics['years']),'ratioFields':len(metrics['fields']),'ratioPresets':len(metrics['presets'])}
    report={'asOf':str(day), 'symbols':len(codes), 'counts':dict(counts),'cardCounts':dict(card_counts),
            'securities':per_symbol,'differences':errors}
    return report


if __name__ == '__main__':
    parser=ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        report=audit(factory,session)
    data=json.dumps(report,ensure_ascii=True,indent=2)
    if args.output:
        args.output.write_text(data+'\n',encoding='utf-8')
    else:
        print(data)
    raise SystemExit(bool(report['differences']))
