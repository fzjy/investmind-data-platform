"""分红全字段请求合同与窄表元数据。"""
DIVIDEND_FIELDS = ('ts_code', 'end_date', 'ann_date', 'div_proc', 'stk_div', 'stk_bo_rate',
    'stk_co_rate', 'cash_div', 'cash_div_tax', 'record_date', 'ex_date', 'pay_date',
    'div_listdate', 'imp_ann_date', 'base_date', 'base_share')
CAPITAL_FIELDS = ('stk_div', 'stk_bo_rate', 'stk_co_rate', 'base_date', 'base_share')


def _metadata(row):
    data = dict(row.data or {})
    # 历史通用_coerce把YYYYMMDD字符串存为整数；仅在明确日期字段上适配。
    for field in ('imp_ann_date', 'base_date'):
        value = data.get(field)
        if type(value) is int and len(str(value)) == 8:
            data[field] = str(value)
    return data
