from news_admin.evaluate import cluster_prf, label_accuracy


def test_cluster_prf_pairwise():
    gold = {'a': 'E1', 'b': 'E1', 'c': 'E2', 'd': 'E3'}
    pred = [['a', 'b', 'c'], ['d']]
    p, r, f1 = cluster_prf(gold, pred)
    assert (p, r) == (1 / 3, 1.0)
    assert round(f1, 3) == 0.5


def test_cluster_prf_perfect_and_all_singletons():
    gold = {'a': 'E1', 'b': 'E1', 'c': 'E2'}
    assert cluster_prf(gold, [['a', 'b'], ['c']]) == (1.0, 1.0, 1.0)
    assert cluster_prf(gold, [['a'], ['b'], ['c']]) == (1.0, 0.0, 0.0)


def test_label_accuracy_reports_per_label_confusions():
    report = label_accuracy({'a': 'economy', 'b': 'politics', 'c': 'economy'}, {'a': 'economy', 'b': 'economy', 'c': 'economy'})
    assert report['accuracy'] == 2 / 3
    assert report['confusions'] == {'politics→economy': 1}
