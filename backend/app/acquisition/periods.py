"""按请求期筛选已有业务值，并核对执行凭证声明的时间范围。"""
import calendar
import re
from datetime import date
from app.acquisition.contracts import QueryError


def bounds(period):
    try:
        if re.fullmatch(r'\d{4}', period):
            year = int(period)
            return date(year,1,1), date(year,12,31)
        if re.fullmatch(r'\d{4}Q[1-4]', period):
            year, quarter = int(period[:4]), int(period[-1])
            month = quarter * 3
            return date(year,month-2,1), date(year,month,calendar.monthrange(year,month)[1])
        if re.fullmatch(r'\d{4}-\d{2}', period):
            year, month = map(int,period.split('-'))
            return date(year,month,1), date(year,month,calendar.monthrange(year,month)[1])
        day = date.fromisoformat(period)
        return day, day
    except (TypeError,ValueError,OverflowError):
        raise QueryError('数据期应为有效日期、月份、季度或年份') from None


def contains(period, value):
    if not value:
        return False
    try:
        low, high = bounds(period)
        start, end = bounds(str(value)[:10])
        return low <= start and end <= high
    except ValueError:
        return False


def covers(scope, observed, period):
    params = scope.get('params',{})
    point = params.get('trade_date') or params.get('period') or scope.get('observedAt')
    if point and contains(period,point):
        return True
    start = params.get('start_date') or scope.get('start')
    end = params.get('end_date') or scope.get('end')
    if not start or not end:
        return False
    try:
        low, high = bounds(str(observed) if observed else period)
        return bounds(start)[0] <= low and high <= bounds(end)[1]
    except ValueError:
        return False
