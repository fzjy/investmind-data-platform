"""BSE缺独立日历时引用SSE，不伪造来源行。"""
def calendar_exchange(exchange: str, available: set[str]) -> str:
    if exchange not in {'SSE', 'SZSE', 'BSE'}:
        raise ValueError('unknown exchange')
    return 'SSE' if exchange == 'BSE' and 'BSE' not in available else exchange


def open_flag(value: object) -> bool:
    if value in (0, '0', False):
        return False
    if value in (1, '1', True):
        return True
    raise ValueError('calendar is_open must be 0 or 1')
