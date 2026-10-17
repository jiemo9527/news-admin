from datetime import datetime, timedelta, timezone

from news_admin.timefmt import format_time

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)  # 北京时间 20:00


def test_exact_time_shows_relative_and_absolute_beijing_time():
    assert format_time(NOW - timedelta(minutes=5), 'exact', NOW) == '5分钟前 · 10-17 19:55'
    assert format_time(NOW - timedelta(hours=3), 'exact', NOW) == '3小时前 · 10-17 17:00'
    assert format_time(NOW - timedelta(minutes=0, seconds=20), 'exact', NOW) == '刚刚 · 10-17 19:59'


def test_date_only_precision_never_invents_a_clock_time():
    assert format_time(datetime(2026, 10, 17, 4, tzinfo=timezone.utc), 'date', NOW) == '今天'
    assert format_time(datetime(2026, 10, 16, 4, tzinfo=timezone.utc), 'date', NOW) == '昨天'
    assert format_time(datetime(2026, 10, 13, 4, tzinfo=timezone.utc), 'date', NOW) == '10-13'


def test_unknown_time():
    assert format_time(None, 'none', NOW) == '发布时间未知'


def test_older_than_a_day_shows_days():
    assert format_time(NOW - timedelta(days=2, hours=1), 'exact', NOW) == '2天前 · 10-15 19:00'


def test_created_and_updated_times():
    from news_admin.timefmt import created_updated
    first = NOW - timedelta(hours=5)
    later = NOW - timedelta(hours=1)
    assert created_updated([(later, 'exact'), (first, 'exact')], NOW) == ('5小时前 · 10-17 15:00', '1小时前 · 10-17 19:00')
    # 相差不足 10 分钟不算更新
    assert created_updated([(first, 'exact'), (first + timedelta(minutes=3), 'exact')], NOW)[1] == ''
    # 只有日期、且与创建同一天的「更新」没有信息量
    assert created_updated([(first, 'exact'), (datetime(2026, 10, 17, 4, tzinfo=timezone.utc), 'date')], NOW)[1] == ''
    assert created_updated([(datetime(2026, 10, 15, 4, tzinfo=timezone.utc), 'date'), (later, 'exact')], NOW) == ('10-15', '1小时前 · 10-17 19:00')
    assert created_updated([], NOW) == ('时间未知', '')
