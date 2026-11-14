"""整体验证后筛可见窗口，调用者另保留原始响应。"""
from datetime import date
from decimal import Decimal
from app.connectors.tushare.contracts import TushareResponse
from app.connectors.tushare.strategies import Dataset
from app.shared.calendar import open_flag
from app.stocks.update.planning import MARKET, FINANCIAL


def strict_date(value, *, required=False):
    if value is None or value == '':
        if required:
            raise ValueError('required date missing')
        return None
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise ValueError('invalid supplier date')
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        raise ValueError('invalid supplier date') from None


def validate_response(plan, response, as_of: date, window_start: date) -> TushareResponse:
    if len(set(response.fields)) != len(response.fields) or not set(plan.fields) <= set(response.fields):
        raise ValueError('response fields mismatch')
    if len(response.items) >= plan.page_size:
        raise ValueError('response reached cap; pagination not verified')
    seen, retained = {}, []
    for row in response.rows():
        dataset = plan.dataset
        future_revision = False
        if dataset == Dataset.TRADE_CALENDAR:
            if row.get('exchange') != plan.params['exchange']:
                raise ValueError('calendar exchange mismatch')
            day = strict_date(row.get('cal_date'), required=True)
            key = (row['exchange'], day)
            if not strict_date(plan.params['start_date']) <= day <= strict_date(plan.params['end_date']):
                raise ValueError('calendar date outside request')
            row['is_open'] = int(open_flag(row.get('is_open')))
            strict_date(row.get('pretrade_date'))
        else:
            if row.get('ts_code') != plan.params['ts_code']:
                raise ValueError('stock response mismatch')
            if dataset == Dataset.DISCLOSURE_DATE:
                value = row.get('modify_date')
                if isinstance(value, str) and ',' in value:
                    revisions = [strict_date(part.strip(), required=True) for part in value.split(',')]
                    row['modify_date'] = max(revisions).strftime('%Y%m%d')
                revised = strict_date(row.get('modify_date'))
                future_revision = revised is not None and revised > as_of
            dates = {f: strict_date(v) for f, v in row.items() if f.endswith('_date')}
            if dataset in MARKET:
                day = dates.get('trade_date')
                if day is None or not window_start <= day <= as_of:
                    raise ValueError('market date outside window')
                if 'start_date' in plan.params and not strict_date(plan.params['start_date']) <= day <= strict_date(plan.params['end_date']):
                    raise ValueError('market date outside request')
                key = (row['ts_code'], day)
                for f in ('close', 'adj_factor'):
                    if f in row and (row[f] is None or Decimal(str(row[f])) <= 0):
                        raise ValueError('market key value missing or nonpositive')
            else:
                end = dates.get('end_date')
                if end is None:
                    raise ValueError('report period missing')
                if 'period' in plan.params and end != strict_date(plan.params['period']):
                    raise ValueError('report period mismatch')
                visible = dates.get('f_ann_date') or dates.get('ann_date')
                if dataset in FINANCIAL and visible is not None and visible < end:
                    raise ValueError('financial disclosure before report')
                if dataset == Dataset.DISCLOSURE_DATE:
                    key = (row['ts_code'], end)
                elif dataset == Dataset.DIVIDEND:
                    key = (row['ts_code'], end, dates.get('ann_date'), row.get('div_proc'))
                else:
                    key = (row['ts_code'], end, row.get('report_type'), dates.get('ann_date'), row.get('update_flag'))
        for value in row.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and not Decimal(str(value)).is_finite():
                raise ValueError('nonfinite provider value')
        # Proposal/pass stages may carry multiple share-base revisions with the
        # same legacy core key. Keep all of them in raw; only implemented events
        # have a canonical core cash-event contract. Never pick an amount.
        if dataset == Dataset.DIVIDEND and row.get('div_proc') != '实施':
            continue
        if key in seen and seen[key] != row:
            raise ValueError('conflicting natural key')
        if key in seen:
            continue
        seen[key] = row
        if dataset != Dataset.TRADE_CALENDAR and dataset not in MARKET:
            window_date = (dates.get('pay_date') or dates.get('ex_date') or dates.get('imp_ann_date') or end) if dataset == Dataset.DIVIDEND else end
            if visible is None or visible > as_of or max(end, window_date) < window_start or future_revision:
                continue
            if dataset == Dataset.DIVIDEND and row.get('div_proc') == '实施':
                implemented = dates.get('imp_ann_date')
                if implemented is None or implemented > as_of:
                    continue
        retained.append(tuple(row.get(f) for f in response.fields))
    if plan.dataset == Dataset.TRADE_CALENDAR:
        start, end = strict_date(plan.params['start_date']), strict_date(plan.params['end_date'])
        if len(seen) != (end-start).days+1:
            raise ValueError('calendar response incomplete')
    return TushareResponse(fields=response.fields, items=tuple(retained))
