"""既有股票发布范围内的研究字段重建；不采集、不重新运行派生流水线。"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone
from sqlalchemy import select,text
from app.db.models import PublishedSnapshot
from app.stocks.derived.inputs import CoreSnapshotRepository
from app.jobs.locks import data_publication_lock_key,job_lock_for_engine
from app.marts.contracts import MartCandidate,MartRows,QualityResult,release_version
from app.marts.dto import validate_product_payload
from app.marts.publication import SnapshotPublisher
from app.stocks.marts.stocks import ScreenerMartBuilder,StockWorkbenchMartBuilder,load_derived_mart_rows,prepare_workbench_inputs

def project_research_snapshots(current,screener,workbenches):
    original={s['symbol']:s for s in current['screener']['stocks']}
    fresh={s['symbol']:s for s in screener.payload['stocks']}
    if set(original)!=set(fresh) or len(original)!=len(current['screener']['stocks']):raise ValueError('STOCK_SCOPE_CHANGED')
    expected={f'stock-workbench:{s}' for s in original}
    if {c.product for c in workbenches}!=expected or not expected.issubset(current):raise ValueError('STOCK_WORKBENCH_SCOPE_CHANGED')
    payload=deepcopy(current['screener']);payload['bootstrap']=deepcopy(screener.payload['bootstrap'])
    for row in payload['stocks']:
        source=fresh[row['symbol']]
        for key in ('listRisks','listMissingReasons','researchValues','financialTrends','listHistory','profitTrend','metricBasis','realMoneyIndex','roic','pePercentile','pbPercentile','institutionHolding','dividendYears','dividendYield','payoutRatio','expectedDividendYield','expectedDividendYieldNTM'):
            if key in source:row[key]=deepcopy(source[key])
    results=[replace(screener,payload=payload,rows=MartRows(screener=tuple(payload['stocks'])))]
    for candidate in workbenches:
        p=deepcopy(current[candidate.product]);fresh=candidate.payload
        p['quote']['marketCap']=fresh['quote'].get('marketCap')
        if 'peTtm' in fresh['quote']:
            p['quote']['peTtm']=fresh['quote']['peTtm']
        if 'cards' in fresh['metrics'] and not p['metrics'].get('cardInputSignature'):
            p['metrics']['cards']=[c for c in p['metrics'].get('cards', []) if c['label']!='账上净现金'] + [
                c for c in fresh['metrics']['cards'] if c['label']=='账上净现金']
        for key in ('fields','years','presets'):p['metrics'][key]=deepcopy(fresh['metrics'][key])
        for key in ('years','reportTypes','announcementDate'):p['financial'][key]=deepcopy(fresh['financial'][key])
        p['financial']['statements']['tables']=deepcopy(fresh['financial']['statements']['tables'])
        p['financial']['dashboard']['trends']=deepcopy(fresh['financial']['dashboard']['trends'])
        if 'bands' in fresh['financial']['dashboard']:
            p['financial']['dashboard']['bands']=deepcopy(fresh['financial']['dashboard']['bands'])
        if 'returnHistory' in fresh['financial']['dashboard']:
            p['financial']['dashboard']['returnHistory']=deepcopy(fresh['financial']['dashboard']['returnHistory'])
        if 'businessHistory' in fresh['financial']['dashboard']:
            p['financial']['dashboard']['businessHistory']=deepcopy(fresh['financial']['dashboard']['businessHistory'])
        if 'operatingHistory' in fresh['financial']['dashboard']:
            p['financial']['dashboard']['operatingHistory']=deepcopy(fresh['financial']['dashboard']['operatingHistory'])
        shareholders=fresh['financial'].get('shareholders',{})
        for key in ('annualDistributions', 'concentrationPeriods'):
            if key in shareholders:p['financial']['shareholders'][key]=deepcopy(shareholders[key])
        if shareholders.get('periods'):
            p['financial']['shareholders'].update(deepcopy(shareholders))
        elif shareholders.get('availability'):
            for key in ('availability','annualReturns','repurchaseEvidence','dividendSeries','returnSeries','concentrationSeries'):
                if key in shareholders:p['financial']['shareholders'][key]=deepcopy(shareholders[key])
        results.append(replace(candidate,payload=p,rows=MartRows(workbenches=(p,))))
    return tuple(results)

def rebuild_stock_research(factory,*,dry_run=True,business_only=False):
    engine=factory.kw['bind'];lock=job_lock_for_engine(engine);key=data_publication_lock_key()
    acquired=not dry_run and lock.try_acquire(key)
    report={'status':'blocked','requestCount':0,'dryRun':dry_run,'promotedProducts':[],'checkedAt':datetime.now(timezone.utc).isoformat()}
    if not dry_run and not acquired:return {**report,'code':'STOCK_PUBLICATION_LOCK_BUSY'}
    try:
        with factory() as session:
            session.execute(text('SET TRANSACTION READ ONLY'))
            base=session.get(PublishedSnapshot,'screener')
            if base is None:return {**report,'code':'STOCK_BASE_REQUIRED'}
            symbols=tuple(s['symbol'] for s in base.payload.get('stocks',[]))
            if not symbols or len(set(symbols))!=len(symbols):return {**report,'code':'STOCK_SCOPE_INVALID'}
            products=('screener',*(f'stock-workbench:{s}' for s in symbols))
            bases={p.product:p for p in session.scalars(select(PublishedSnapshot).where(PublishedSnapshot.product.in_(products)))}
            if set(bases)!=set(products) or any(p.as_of.date()!=base.as_of.date() for p in bases.values()):return {**report,'code':'STOCK_BASE_DATE_OR_SCOPE_MISMATCH'}
            as_of=base.as_of.date();previous={k:p.version for k,p in bases.items()};current={k:deepcopy(p.payload) for k,p in bases.items()}
            stale={k:(p.stale,p.stale_reason) for k,p in bases.items()}
        if business_only:
            from app.stocks.business_projection import load_business_histories
            histories = load_business_histories(factory, symbols, as_of)
            if set(histories) != set(symbols):
                return {**report, 'code': 'BUSINESS_EVIDENCE_SCOPE_INCOMPLETE'}
            candidates = []
            for symbol in symbols:
                product = f'stock-workbench:{symbol}'
                payload = deepcopy(current[product])
                payload['financial']['dashboard']['businessHistory'] = histories[symbol]
                rows = MartRows(workbenches=(payload,), charts=tuple(payload['dailyChart']['candles']))
                candidates.append(MartCandidate(product, bases[product].as_of, payload, rows))
        else:
            snapshot=CoreSnapshotRepository(factory).load(as_of,symbols)
            snapshot=replace(snapshot,securities=tuple(s for s in snapshot.securities if s.ts_code in set(symbols)))
            derived=load_derived_mart_rows(factory,symbols,as_of);prepared=prepare_workbench_inputs(snapshot,factory)
            screen=ScreenerMartBuilder(factory).build(snapshot,'research-preview',derived_rows=derived)
            workbenches=[StockWorkbenchMartBuilder(factory).build(snapshot,s,'research-preview',derived_rows=derived,prepared_inputs=prepared) for s in symbols]
            candidates=project_research_snapshots(current,screen,workbenches)
        from app.stocks.marts.risk_cards import project_overlay_risks
        with factory() as session:
            candidates = project_overlay_risks(session, candidates, previous)
            if not business_only:
                from app.stocks.marts.shareholder_returns import project_returns
                projected = {c.product: c for c in project_returns(session, [c for c in candidates if c.product.startswith("stock-workbench:")])}
                candidates = tuple(projected.get(c.product, c) for c in candidates)
        if business_only and any(c.payload.get('risks') != current[c.product].get('risks') for c in candidates):
            return {**report, 'code': 'STOCK_RISK_INPUTS_CHANGED'}
        candidates=tuple(replace(c,stale=stale[c.product][0],stale_reason=stale[c.product][1]) for c in candidates)
        errors={c.product:validate_product_payload(c.product,c.payload) for c in candidates}
        workbench_count = sum(c.product.startswith('stock-workbench:') for c in candidates)
        checks=(QualityResult('stock-research-dto','blocking',not any(errors.values()),{'errors':{k:v for k,v in errors.items() if v}}),QualityResult('stock-research-scope','blocking',len(candidates)==(len(symbols) if business_only else len(products)),{'symbols':len(symbols),'workbenches':workbench_count}))
        if any(not c.passed for c in checks):return {**report,'code':'STOCK_RESEARCH_QUALITY_BLOCKED','quality':[c.details for c in checks]}
        if business_only:
            changed_candidates = tuple(c for c in candidates if current[c.product] != c.payload)
            if not changed_candidates:
                return {**report, 'status': 'planned' if dry_run else 'no_change', 'wouldPublish': False,
                        'symbolCount': len(symbols), 'workbenchCount': workbench_count, 'changedCount': 0}
            candidates = changed_candidates
            report['changedCount'] = len(candidates)
        version=release_version(as_of,candidates)
        report.update(asOf=as_of.isoformat(),version=version,symbolCount=len(symbols),workbenchCount=workbench_count,products=[c.product for c in candidates],previousVersions=previous)
        changed=any(current[c.product]!=c.payload for c in candidates)
        if dry_run:return {**report,'status':'planned','wouldPublish':changed}
        # 再次核对冻结的发布父，防止等待/预检期间用户更新后被覆盖。
        with factory() as session:
            now={p.product:p.version for p in session.scalars(select(PublishedSnapshot).where(PublishedSnapshot.product.in_(products)))}
        if now!=previous:return {**report,'code':'STOCK_BASE_CHANGED'}
        if not changed:return {**report,'status':'no_change'}
        outcome=SnapshotPublisher(factory).publish_many(candidates,version,checks,expected_versions=previous,expected_freshness=stale,preserve_freshness_check=business_only)
        return {**report,'status':'completed','promotedProducts':list(outcome.promoted_products)}
    finally:
        if acquired:lock.release(key)
