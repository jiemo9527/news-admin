from news_admin.cluster import cosine
from news_admin.embeddings import HashingEmbedder


def test_hashing_embedder_is_deterministic_and_similarity_tracks_overlap():
    embed = HashingEmbedder()
    a, b, c = embed(['美联储宣布降息25个基点', '美联储周三宣布降息25个基点', '日本东北部发生6.1级地震'])
    assert embed(['美联储宣布降息25个基点'])[0] == a
    assert cosine(a, b) > 0.6
    assert cosine(a, c) < 0.2
