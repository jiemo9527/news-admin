from news_admin.digest import build_sections, render_text
from news_admin.models import Article, ArticleAnalysis, Event, EventSummary


def ev(id, headline, topic, region='international', score=5.0, update=False, degraded=False, conflicts=()):
    a = Article(id=id, source=f'S{id}', url=f'https://x/{id}', title=headline)
    an = ArticleAnalysis(title_zh=headline, region=region, topic=topic, topic_confidence=0.9, content_type='news', has_new_development=True, entities=[],
                         key_facts=['f'], event_fingerprint='x', impact=3, impact_reason='r')
    s = EventSummary.model_validate({'headline': headline, 'sentences': [{'text': f'{headline}第一句。', 'sources': [1]}], 'conflicts': list(conflicts)})
    return Event(id=id, articles=[a], analyses={id: an}, summary=s, score=score, is_update=update, summary_degraded=degraded)


def test_top_section_takes_highest_scores_and_rest_grouped_by_topic():
    events = [ev(str(i), f'事件{i}', 'economy' if i % 2 else 'technology', score=10 - i) for i in range(8)]
    sections = build_sections(events, top_n=3)
    assert sections[0].title == '要闻'
    assert [e.id for e in sections[0].events] == ['0', '1', '2']
    titles = [s.title for s in sections[1:]]
    assert titles == ['宏观经济与金融', '科技']
    assert all(e.id not in {'0', '1', '2'} for s in sections[1:] for e in s.events)


def test_text_digest_shows_sources_update_tag_and_conflicts():
    text = render_text([ev('1', '美联储降息', 'economy', update=True, conflicts=['A称25基点；B称50基点'])], web_url='https://n/p/1', title='简报')
    assert '【更新】美联储降息' in text
    assert '来源：S1' in text
    assert '说法不一：A称25基点；B称50基点' in text
    assert text.rstrip().endswith('https://n/p/1')
