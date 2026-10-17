from news_admin.dedupe import dedupe_articles, text_similarity
from news_admin.models import Article

BODY = '国家统计局今天发布数据，今年前三季度国内生产总值同比增长百分之五点二，其中第三季度增长百分之四点九，消费对经济增长的贡献率继续提升。'


def art(id, url, body, source='A', title='标题'):
    return Article(id=id, source=source, url=url, title=title, body=body)


def test_text_similarity_is_high_for_syndicated_copies_and_low_for_different_text():
    other = '美国航空航天局宣布新一代登月火箭完成全系统点火测试，计划明年执行载人绕月任务，宇航员训练已进入最后阶段。'
    assert text_similarity(BODY, BODY.replace('今天', '17日')) >= 0.8
    assert text_similarity(BODY, BODY + '记者张三报道。') >= 0.8
    assert text_similarity(BODY, other) < 0.1


def test_dedupe_merges_same_canonical_url():
    kept = dedupe_articles([art('1', 'https://a.com/x?utm_source=t', BODY), art('2', 'https://www.a.com/x/', BODY[:40], source='B')])
    assert [a.id for a in kept] == ['1']
    assert kept[0].duplicate_sources == ['B']


def test_dedupe_keeps_the_longest_copy_of_a_syndicated_story():
    short = art('1', 'https://a.com/1', BODY.replace('今天', '17日'), source='A')
    long = art('2', 'https://b.com/2', BODY + '记者张三报道。', source='B')
    kept = dedupe_articles([short, long])
    assert [a.id for a in kept] == ['2']
    assert kept[0].duplicate_sources == ['A']


def test_dedupe_keeps_different_stories():
    other = '美国航空航天局宣布新一代登月火箭完成全系统点火测试，计划明年执行载人绕月任务，宇航员训练已进入最后阶段。'
    assert len(dedupe_articles([art('1', 'https://a.com/1', BODY), art('2', 'https://a.com/2', other)])) == 2
