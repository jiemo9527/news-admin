from datetime import datetime, timedelta

from news_admin.cluster import ClusterConfig, cluster_articles, cosine
from news_admin.models import Article

T0 = datetime(2026, 10, 17, 8)


def art(id, hours=0):
    return Article(id=id, source=id, url=f'https://x/{id}', title=id, body='', published_at=T0 + timedelta(hours=hours))


def embedder(vectors):
    return lambda texts: [vectors[t.split('\n')[0]] for t in texts]


def test_cosine():
    assert cosine([1, 0], [1, 0]) == 1.0
    assert abs(cosine([1, 0], [0, 1])) < 1e-9


def test_high_similarity_merges_without_asking_llm():
    asked = []
    groups = cluster_articles(
        [art('a'), art('b')],
        embed=embedder({'a': [1, 0], 'b': [0.99, 0.05]}),
        judge=lambda x, y: asked.append(1) or False,
        config=ClusterConfig(high=0.9, low=0.6),
    )
    assert [[a.id for a in g] for g in groups] == [['a', 'b']]
    assert asked == []


def test_gray_zone_is_decided_by_llm_judge():
    vectors = {'a': [1, 0], 'b': [0.75, 0.66], 'c': [0.75, -0.66]}
    groups = cluster_articles(
        [art('a'), art('b'), art('c')],
        embed=embedder(vectors),
        judge=lambda x, y: {x.id, y.id} == {'a', 'b'},
        config=ClusterConfig(high=0.9, low=0.6),
    )
    assert sorted(sorted(a.id for a in g) for g in groups) == [['a', 'b'], ['c']]


def test_low_similarity_never_merges_and_never_asks():
    asked = []
    groups = cluster_articles(
        [art('a'), art('b')],
        embed=embedder({'a': [1, 0], 'b': [0, 1]}),
        judge=lambda x, y: asked.append(1) or True,
        config=ClusterConfig(high=0.9, low=0.6),
    )
    assert len(groups) == 2 and asked == []


def test_articles_outside_time_window_are_not_merged():
    groups = cluster_articles(
        [art('a'), art('b', hours=72)],
        embed=embedder({'a': [1, 0], 'b': [1, 0]}),
        judge=lambda x, y: True,
        config=ClusterConfig(high=0.9, low=0.6, window_hours=48),
    )
    assert len(groups) == 2


def test_groups_are_ordered_by_size_then_first_seen():
    vectors = {'a': [1, 0], 'b': [0, 1], 'c': [0.01, 1]}
    groups = cluster_articles([art('a'), art('b'), art('c')], embed=embedder(vectors), judge=lambda x, y: False, config=ClusterConfig())
    assert [[a.id for a in g] for g in groups] == [['b', 'c'], ['a']]


def test_custom_text_function_is_used_for_embedding():
    seen = []

    def embed(texts):
        seen.extend(texts)
        return [[1, 0] for _ in texts]

    cluster_articles([art('a'), art('b')], embed=embed, judge=lambda x, y: True, config=ClusterConfig(), text_of=lambda a: f'norm-{a.id}')
    assert seen == ['norm-a', 'norm-b']
