"""官方年度文档的闭合计划；没有网络、数据库或可覆盖的来源参数。"""
from app.macro.extensions.config import ExtensionError
from app.macro.extensions.plan import digest

SOURCE_URL = 'https://www.pbc.gov.cn/diaochatongjisi/116219/116319/index.html'
SERIES_ID = 'cn.fiscal.government_bond_net_financing'


def month_label(index):
    year, month = divmod(index, 12)
    if not 1000 <= year <= 9999:
        raise ExtensionError('EXTENSION_PLAN_INVALID')
    return f'{year:04}-{month + 1:02}-01'


def build_government_plan(group, policy, due):
    last = due.year * 12 + due.month - 2
    first = last - group.lookbackPeriods + 1
    years = range(first // 12, last // 12 + 1)
    configured = {d.year: d.url for d in group.documents}
    if any(year not in configured for year in years) or len(years) > group.limits.maxHttpPerPeriod:
        raise ExtensionError('EXTENSION_PLAN_INVALID')
    result = {'schemaVersion': 1, 'normalizerVersion': 'official-government-v1',
        'groupId': 'governmentBonds', 'groupPolicy': policy,
        'groupPolicyHash': digest({'policy': policy, 'normalizer': 'official-government-v1'}),
        'scheduledAt': due.isoformat(), 'startPeriod': month_label(first), 'endPeriod': month_label(last),
        'seriesId': SERIES_ID, 'source': 'pboc', 'sourceUrl': SOURCE_URL, 'region': 'CN',
        'unit': '亿元', 'frequency': 'monthly', 'periodTimezone': 'Asia/Shanghai',
        'documents': [{'year': year, 'url': configured[year]} for year in years],
        'limits': group.limits.model_dump()}
    return {**result, 'fingerprint': digest(result)}
