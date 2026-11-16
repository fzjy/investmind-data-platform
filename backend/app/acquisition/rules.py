from app.acquisition.contracts import Assessment, Availability, Evidence


def classify(availability: Availability, evidence: Evidence) -> Assessment:
    if evidence.status in ('paused', 'running'):
        return Assessment('excluded', evidence.reason or {'paused':'来源已暂停', 'running':'正在执行'}[evidence.status])
    if availability.kind in ('unknown', 'not_applicable'):
        return Assessment('excluded', availability.reason or '无法确认适用范围')
    failure = '，'.join(filter(None, (evidence.reason, evidence.error_code)))
    if availability.kind == 'missing':
        reason = availability.reason or '没有可用数据'
        if evidence.status == 'failed':
            reason += '；最近获取失败：' + failure
        return Assessment('missing', reason)
    if evidence.status == 'failed':
        return Assessment('failed', failure)
    if evidence.status == 'success':
        return Assessment('normal', availability.reason or '有可用数据，采集凭证已确认')
    return Assessment('excluded', evidence.reason or '有可用数据，缺少可信执行记录')


def classify_derived(availability: Availability, dependencies: tuple[Assessment,...]) -> Assessment:
    if availability.kind == 'missing':
        return Assessment('missing', availability.reason or '缺少可用计算结果')
    if availability.kind in ('unknown', 'not_applicable') or not dependencies:
        return Assessment('excluded', availability.reason or '未确认计算范围或必要输入')
    for status in ('failed', 'missing', 'excluded'):
        reasons = [item.reason for item in dependencies if item.status == status]
        if reasons:
            return Assessment(status, '必要输入：' + '；'.join(dict.fromkeys(reasons)))
    return Assessment('normal', '计算结果可用，必要输入均正常')


def summarize(items: list[Assessment]) -> dict[str,int]:
    counts = dict.fromkeys(('normal','failed','missing','excluded'), 0)
    for item in items:
        counts[item.status] += 1
    return counts
