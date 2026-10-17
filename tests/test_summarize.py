import re
from news_admin.models import Article, EventSummary
from news_admin.summarize import fallback_summary, validate_summary

ARTS = [
    Article(id='a', source='新华社', url='https://x/1', title='前三季度GDP增长5.2%', body='国家统计局发布，前三季度GDP同比增长5.2%，三季度增长4.9%。'),
    Article(id='b', source='路透社', url='https://y/2', title='China Q3 growth', body='China economy grew 4.9% in the third quarter, statistics bureau said on Friday.'),
]


def summary(sentences, conflicts=()):
    return EventSummary.model_validate({'headline': '前三季度GDP增长5.2%', 'sentences': sentences, 'conflicts': list(conflicts)})


def test_valid_summary_passes():
    s = summary([{'text': '前三季度GDP同比增长5.2%。', 'sources': [1]}, {'text': '三季度增长4.9%。', 'sources': [1, 2]}])
    assert validate_summary(s, ARTS) == []


def test_citation_outside_source_range_is_rejected():
    errors = validate_summary(summary([{'text': '前三季度GDP同比增长5.2%。', 'sources': [3]}]), ARTS)
    assert any('来源编号' in e for e in errors)


def test_number_not_found_in_cited_sources_is_rejected():
    errors = validate_summary(summary([{'text': '前三季度GDP同比增长5.6%。', 'sources': [1]}]), ARTS)
    assert any('5.6' in e for e in errors)


def test_number_must_appear_in_the_cited_source_not_just_any_source():
    errors = validate_summary(summary([{'text': '三季度增长4.9%，前三季度增长5.2%。', 'sources': [2]}]), ARTS)
    assert any('5.2' in e for e in errors)


def test_chinese_number_words_are_not_flagged_and_years_are_checked():
    ok = summary([{'text': '统计局在周五公布了两项数据。', 'sources': [2]}])
    assert validate_summary(ok, ARTS) == []
    bad = summary([{'text': '2023年前三季度GDP增长5.2%。', 'sources': [1]}])
    assert any('2023' in e for e in validate_summary(bad, ARTS))


def test_filler_phrases_are_rejected():
    errors = validate_summary(summary([{'text': '前三季度GDP增长5.2%，值得关注。', 'sources': [1]}]), ARTS)
    assert any('值得关注' in e for e in errors)


def test_fallback_summary_uses_lead_source_and_cites_it():
    s = fallback_summary(ARTS, title='前三季度GDP增长5.2%')
    assert s.headline == '前三季度GDP增长5.2%'
    assert s.sentences[0].sources == [1]
    assert '5.2%' in s.sentences[0].text


EN = [Article(id='e', source='BBC', url='https://x/3', title='Amazon deforestation falls',
              body='Deforestation fell 41.8% in the year to July, the lowest since records began in 1988. The law was signed in September and takes effect on 4 December. Three children and one teacher died; nine others were hurt.')]


def test_month_numbers_are_accepted_when_english_source_names_the_month():
    ok = summary([{'text': '截至7月的一年间毁林率下降41.8%，法案9月签署，12月4日生效。', 'sources': [1]}])
    assert validate_summary(ok, EN) == []


def test_english_number_words_count_as_evidence():
    ok = summary([{'text': '事件造成3名儿童和1名教师死亡，另有9人受伤。', 'sources': [1]}])
    assert validate_summary(ok, EN) == []
    bad = summary([{'text': '事件造成5人死亡。', 'sources': [1]}])
    assert validate_summary(bad, EN)


def test_fallback_prefers_chinese_key_facts_that_pass_the_number_check():
    from news_admin.models import ArticleAnalysis
    an = ArticleAnalysis(title_zh='亚马孙毁林率创新低', region='international', topic='environment', topic_confidence=0.9, content_type='news', has_new_development=True,
                         entities=[], key_facts=['毁林率下降50%。', '毁林率下降41.8%，为1988年以来最低。'], event_fingerprint='x', impact=3, impact_reason='r')
    s = fallback_summary(EN, title='亚马孙毁林率创新低', analysis=an)
    assert [x.text for x in s.sentences] == ['毁林率下降41.8%，为1988年以来最低。']



def test_abbreviated_year_range_counts_as_evidence():
    src = [Article(id='a', source='s', url='u', title='t', body='The previous low was 4,571 sq km in 2011-12.')]
    assert validate_summary(summary([{'text': '此前最低为2011至2012年的4571平方公里。', 'sources': [1]}]), src) == []


def test_fallback_never_returns_foreign_language_text_when_analysis_exists():
    from news_admin.models import ArticleAnalysis
    an = ArticleAnalysis(title_zh='亚马孙毁林率创新低', region='international', topic='environment', topic_confidence=0.9, content_type='news',
                         has_new_development=True, entities=[], key_facts=['毁林率下降50%。'], event_fingerprint='x', impact=3, impact_reason='r')
    s = fallback_summary(EN, title='亚马孙毁林率创新低', analysis=an)
    assert all(re.search(r'[\u4e00-\u9fff]', x.text) for x in s.sentences)


def test_repair_rewrites_stored_english_fallbacks():
    from news_admin.models import ArticleAnalysis, Event, SummarySentence
    from news_admin.summarize import repair_degraded
    an = ArticleAnalysis(title_zh='亚马孙毁林率创新低', region='international', topic='environment', topic_confidence=0.9, content_type='news',
                         has_new_development=True, entities=[], key_facts=['毁林率下降41.8%，为1988年以来最低。'], event_fingerprint='x', impact=3, impact_reason='r')
    lead = EN[0]
    old = EventSummary(headline='亚马孙毁林率创新低', sentences=[SummarySentence(text=lead.body[:60], sources=[1])])
    event = Event(id='e', articles=[lead], analyses={lead.id: an}, summary=old, summary_degraded=True)
    assert repair_degraded(event) is True
    assert event.summary.sentences[0].text == '毁林率下降41.8%，为1988年以来最低。'
    assert repair_degraded(event) is False
