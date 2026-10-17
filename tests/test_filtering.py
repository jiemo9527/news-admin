from news_admin.filtering import FilterConfig, filter_reason
from news_admin.models import ArticleAnalysis


def analysis(**kw):
    base = dict(title_zh='t', region='domestic', topic='society', topic_confidence=0.9, content_type='news', has_new_development=True,
                entities=[], key_facts=['f'], event_fingerprint='x', impact=3, impact_reason='r')
    base.update(kw)
    return ArticleAnalysis(**base)


def test_background_feature_without_news_is_dropped():
    # 「长江名称历经演变」：科普/历史回顾，没有新发生的事实
    reason = filter_reason(analysis(content_type='feature', has_new_development=False, impact=1))
    assert reason and '无新进展' in reason


def test_promo_with_low_impact_is_dropped():
    assert filter_reason(analysis(content_type='promo', impact=2))


def test_low_impact_is_dropped_by_threshold():
    assert filter_reason(analysis(impact=1))
    assert filter_reason(analysis(impact=2)) is None
    assert filter_reason(analysis(impact=2), FilterConfig(min_impact=3))


def test_important_opinion_and_feature_with_news_are_kept():
    assert filter_reason(analysis(content_type='opinion', impact=4)) is None
    assert filter_reason(analysis(content_type='feature', has_new_development=True, impact=3)) is None


def test_opinion_can_be_excluded_entirely():
    assert filter_reason(analysis(content_type='opinion', impact=5), FilterConfig(drop_opinion=True))
