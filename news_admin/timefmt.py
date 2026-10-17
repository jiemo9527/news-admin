"""发布时间展示：精确时间显示「相对时间 · 北京时间」，只有日期的绝不编造时刻。

time_precision：
- exact  来源给出了具体时刻（RSS pubDate、article:published_time、央视 focus_date）
- date   只知道日期（网页 publishdate、URL 中的日期），时刻未知
- none   无法确定
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


def _relative(delta_seconds: float) -> str:
    if delta_seconds < 60:
        return '刚刚'
    minutes = int(delta_seconds // 60)
    if minutes < 60:
        return f'{minutes}分钟前'
    hours = minutes // 60
    if hours < 24:
        return f'{hours}小时前'
    return f'{hours // 24}天前'


def format_time(value: datetime | None, precision: str, now: datetime | None = None) -> str:
    if value is None or precision == 'none':
        return '发布时间未知'
    now = now or datetime.now(timezone.utc)
    local = value.astimezone(SHANGHAI)
    if precision == 'date':
        days = (now.astimezone(SHANGHAI).date() - local.date()).days
        label = {0: '今天', 1: '昨天'}.get(days, local.strftime('%m-%d'))
        return f'{label}（未注明时刻）'
    return f'{_relative((now - value).total_seconds())} · {local.strftime("%m-%d %H:%M")}'
