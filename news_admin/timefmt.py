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
        # 只有日期：只写日期，不写时刻，也不加说明
        return {0: '今天', 1: '昨天'}.get(days, local.strftime('%m-%d'))
    return f'{_relative((now - value).total_seconds())} · {local.strftime("%m-%d %H:%M")}'


UPDATE_MIN_GAP_SECONDS = 600


def created_updated(points: list[tuple[datetime, str]], now: datetime | None = None) -> tuple[str, str]:
    """一组 (时间, 精度) → (创建时间文字, 更新时间文字)；没有可信的更新时间时第二项为空串。

    - 创建 = 最早的时间，更新 = 最晚的时间（来自后续报道或页面 modified_time）
    - 两者相差不足 10 分钟、或更新时间只有日期且与创建同一天 → 不显示更新时间
    """
    known = [(t, p) for t, p in points if t is not None and p != 'none']
    # 只有日期的时间点按北京时间正午存放，顺序不可信：同一天已有精确时刻时，丢弃它
    exact_days = {t.astimezone(SHANGHAI).date() for t, p in known if p == 'exact'}
    known = [(t, p) for t, p in known if p == 'exact' or t.astimezone(SHANGHAI).date() not in exact_days]
    if not known:
        return '时间未知', ''
    created = min(known, key=lambda x: x[0])
    updated = max(known, key=lambda x: x[0])
    created_text = format_time(created[0], created[1], now)
    gap = (updated[0] - created[0]).total_seconds()
    same_day = updated[0].astimezone(SHANGHAI).date() == created[0].astimezone(SHANGHAI).date()
    if gap < UPDATE_MIN_GAP_SECONDS or (updated[1] == 'date' and same_day):
        return created_text, ''
    updated_text = format_time(updated[0], updated[1], now)
    return created_text, '' if updated_text == created_text else updated_text
