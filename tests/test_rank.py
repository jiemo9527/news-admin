from datetime import datetime, timedelta

from news_admin.models import Article, ArticleAnalysis, Event
from news_admin.rank import score_event

NOW = datetime(2026, 10, 17, 12)


def analysis(impact, opinion=False):
    return ArticleAnalysis(
        title_zh='t', region='domestic', topic='economy', topic_confidence=0.9, is_opinion=opinion,
        entities=[], key_facts=['f'], event_fingerprint='a|b|c|d', impact=impact, impact_reason='r',
    )


def event(impact, sources=1, hours_ago=0, opinion=False, weight=1.0):
    arts = [
        Article(id=str(i), source=f's{i}', url=f'https://x/{i}', title='t', published_at=NOW - timedelta(hours=hours_ago), source_weight=weight)
        for i in range(sources)
    ]
    return Event(id='e', articles=arts, analyses={a.id: analysis(impact, opinion) for a in arts})


def test_higher_impact_ranks_higher():
    assert score_event(event(5), NOW) > score_event(event(3), NOW)


def test_more_independent_sources_rank_higher_with_diminishing_returns():
    one, two, eight, nine = (score_event(event(3, n), NOW) for n in (1, 2, 8, 9))
    assert two > one and nine > eight
    assert two - one > nine - eight


def test_older_events_decay():
    assert score_event(event(3, hours_ago=1), NOW) > score_event(event(3, hours_ago=30), NOW)


def test_opinion_pieces_are_penalized():
    assert score_event(event(4, opinion=True), NOW) < score_event(event(4), NOW)


def test_one_extra_source_does_not_beat_a_full_impact_level():
    assert score_event(event(4, sources=1), NOW) > score_event(event(3, sources=2), NOW)
