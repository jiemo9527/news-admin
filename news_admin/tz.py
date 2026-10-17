"""全系统统一使用东八区（Asia/Shanghai）时间：存储、展示、调度、抓取窗口都以北京时间为准。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
FETCH_WINDOWS = {
    'today_yesterday': '昨天 0 点至今（北京时间）',
    '24h': '最近 24 小时',
    '7d': '7×24 小时内（低速模式）',
}


def now() -> datetime:
    return datetime.now(TZ)


def to_local(value: datetime) -> datetime:
    """转为北京时间；无时区信息的时间按北京时间理解。"""
    return value.replace(tzinfo=TZ) if value.tzinfo is None else value.astimezone(TZ)


def iso(value: datetime | None = None) -> str:
    return to_local(value or now()).isoformat(timespec='seconds')


def parse(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return to_local(datetime.fromisoformat(text.replace('Z', '+00:00')))
    except ValueError:
        return None


def day_start(value: datetime | None = None, days_ago: int = 0) -> datetime:
    local = to_local(value or now())
    return local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)


def fetch_since(window: str, value: datetime | None = None) -> datetime:
    """抓取时间窗起点：today_yesterday = 昨天 0 点；24h = 24 小时前。"""
    current = to_local(value or now())
    if window == '7d':
        return current - timedelta(hours=7 * 24)
    if window == '24h':
        return current - timedelta(hours=24)
    return day_start(current, days_ago=1)
