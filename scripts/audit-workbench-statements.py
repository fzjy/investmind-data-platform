"""逐格对照冻结的财报原件与已发布工作台；不构建或发布快照。"""
import argparse
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sqlalchemy import text
from app.config import settings
from app.shared.readonly_database import open_readonly_factory
from app.stocks.financial_runtime import workbench_points


KINDS = {'income': 'core.income_statement', 'balance': 'core.balance_sheet', 'cashflow': 'core.cashflow_statement'}
META = {'ts_code','end_date','ann_date','f_ann_date','comp_type','report_type','update_flag','sourceId'}
DERIVED = {'income': ('gross_profit', 'revenue', 'oper_cost'),
           'cashflow': ('free_cash_flow', 'n_cashflow_act', 'c_pay_acq_const_fiolta')}
PERIODS = {'03-31':'Q1', '06-30':'H1', '09-30':'Q3', '12-31':'FY'}


def revision(point):
    return point.visible_at, str(point.values.get('update_flag') or ''), point.announcement_date or date.min


def number(value):
    return Decimal(str(value)) if value is not None and value != '' else None


def compare_table(symbol, kind, table, groups, cutoff, by_field, errors):
    prefix = (symbol, kind)

    def check(actual, expected, *key):
        if actual != expected:
            errors.append([*prefix, *key, str(actual)[:200], str(expected)[:200]])

    periods = sorted(groups)
    all_points = [p for rows in groups.values() for p in rows]
    source_fields = {k for p in all_points for k in p.values if k not in META and not k.startswith('_')}
    derived = DERIVED.get(kind)
    fields = sorted(source_fields | ({derived[0]} if derived else set()))
    check(table.get('amountUnit'), 'yuan', 'amountUnit')
    check(table.get('evidenceAsOf'), min((p.values['_sourceAsOf'] for p in all_points if '_sourceAsOf' in p.values), default=''), 'evidenceAsOf')
    check(table['periods'], periods, 'periods')
    index = {p:i for i,p in enumerate(table['periods'])}
    rows = {row['label']:row for row in table['rows']}
    check(sorted(row['label'] for row in table['rows']), fields, 'fieldSet')
    for key, row in rows.items():
        check(len(row['values']), len(index), key, 'cellCount')
        check((row['yoy'], row['qoq']), ([], []), key, 'publishedChangeArrays')
    check([m['period'] for m in table['reportMetadata']], periods, 'reportMetadata.periods')
    metadata = {m['period']:m for m in table['reportMetadata']}
    summary = Counter()
    by_term = Counter()
    source_counts = Counter()
    report_sources = {}
    for period, revisions in sorted(groups.items()):
        term = PERIODS.get(period[5:], 'other')
        by_term[term] += 1
        rank = max(map(revision, revisions))
        selected = [p for p in revisions if revision(p) == rank]
        assert period <= cutoff and rank[0].isoformat() <= cutoff
        period_meta = metadata.get(period, {})
        check(period_meta.get('announcementDate'), rank[0].isoformat(), period, 'announcementDate')
        check(period_meta.get('originalAnnouncementDate'), rank[2].isoformat() if rank[2] != date.min else None,
              period, 'originalAnnouncementDate')
        check(period_meta.get('updateFlag'), rank[1], period, 'updateFlag')
        check(period_meta.get('reportType'), '1', period, 'reportType')
        ids = sorted({p.values.get('_sourceId') or p.source_record_key for p in selected})
        report_sources[period] = ' | '.join([rank[0].isoformat(), ', '.join(ids)])
        check(sorted(period_meta.get('sourceIds', [])), ids, period, 'sourceIds')
        check(sorted(period_meta.get('capturedAt', [])),
              sorted({str(p.values.get('_capturedAt') or '') for p in selected}), period, 'capturedAt')
        for source in ids:
            source_counts['rawReferences' if source.startswith('raw.') else 'coreReferences'] += 1
        observed = {key:{number(p.values[key]) for p in selected if key in p.values} for key in source_fields}
        conflict = sorted(k for k, v in observed.items() if len(v) > 1)
        check(sorted(period_meta.get('conflictFields', [])), conflict, period, 'conflictFields')
        check(period_meta.get('conflict'), bool(conflict), period, 'conflict')
        if kind == 'cashflow':
            check(sorted(period_meta.get('requestedFields', [])), sorted(k for k,v in observed.items() if v),
                  period, 'requestedFields')
        if kind == 'balance':
            classes = {str(p.values.get('comp_type') or '') for p in selected}
            check(period_meta.get('companyType'), next(iter(classes)) if len(classes)==1 and '' not in classes else None,
                  period, 'companyType')
        values = {key: next(iter(obs)) if len(obs) == 1 else None for key,obs in observed.items()}
        if derived:
            key, left, right = derived
            a,b = values.get(left),values.get(right)
            values[key] = a - b if a is not None and b is not None else None
        for key in fields:
            value = values[key]
            column = index.get(period)
            published = rows.get(key, {}).get('values', [])
            if column is None or column >= len(published):
                check('missing', 'present', key, period)
                continue
            # 原始及派生值以 JS Number 发布，使用相同数值表示逐项比较。
            original = published[column]
            expected = float(value) if value is not None else None
            if derived and key == derived[0] and value is not None:
                expected = float(values[derived[1]]) - float(values[derived[2]])
            check(original, expected, key, period)
            if value is not None:
                category = 'derivedValue' if derived and key == derived[0] else 'sourceValue'
            elif derived and key == derived[0]:
                category = 'derivedMissingInput'
            elif key in conflict:
                category = 'conflictingSources'
            else:
                category = 'sourceNull' if observed[key] else 'absentField'
            by_field[(kind,key)][category] += 1
            summary[category] += 1
    return {'firstPeriod':periods[0] if periods else None, 'lastPeriod':periods[-1] if periods else None,
            'periods':dict(by_term), 'outsideStandardQuarterEnds':[p for p in periods if p[5:] not in PERIODS],
            'fields':len(fields), 'cellCounts':dict(summary), **source_counts,
            'reportSources':report_sources}


def audit(factory, session):
    assert session.scalar(text('show transaction_read_only')) == 'on'
    published = session.execute(text("""
        select product, (as_of at time zone 'UTC')::date, version,
               payload->'financial'->'statements'->>'dataSupply', payload->'financial'->'statements'->'tables'
        from mart.published_snapshot where product like 'stock-workbench:%' order by product
    """)).all()
    codes = [product.split(':')[1] for product,*_ in published]
    assert codes
    errors, facts, by_field = [], {}, defaultdict(Counter)
    for cutoff in sorted({p[1] for p in published}):
        points = workbench_points(factory, cutoff, tuple(codes))
        assert points, '缺可校验的财报原件'
        groups = defaultdict(lambda:defaultdict(list))
        for p in points:
            if p.report_type == '1' and p.visible_at and p.visible_at <= cutoff and p.end_date <= cutoff and (p.announcement_date or date.min) <= cutoff:
                groups[(p.ts_code, p.source_table)][p.end_date.isoformat()].append(p)
        for product,day,version,supply,tables in published:
            if day != cutoff:
                continue
            code = product.split(':',1)[1]
            assert supply == 'verified-raw-statements' and sorted(t['kind'] for t in tables) == sorted(KINDS), code
            facts[code] = {'asOf':str(day), 'version':version, 'tables':{}}
            for table in tables:
                kind = table['kind']
                facts[code]['tables'][kind] = compare_table(code, kind, table, groups[(code,KINDS[kind])],
                                                            str(day), by_field, errors)
    return {'codeVersion': 'verified-raw-statements', 'securityCount':len(facts),
            'tables':{kind:{key:dict(count) for (k,key),count in sorted(by_field.items()) if k==kind} for kind in KINDS},
            'securities':facts, 'differenceCount':len(errors), 'firstDifferences':errors[:50]}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    with open_readonly_factory(settings.database_read_url) as factory, factory() as session:
        report = audit(factory, session)
    payload = json.dumps(report, ensure_ascii=True, indent=2)
    if args.output:
        args.output.write_text(payload + '\n', encoding='utf-8')
    else:
        print(payload)
    raise SystemExit(bool(report['differenceCount']))
