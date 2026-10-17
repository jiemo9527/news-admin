import json

import httpx

from news_admin.analyze import analyze_articles, make_judge
from news_admin.embeddings import EmbeddingClient
from news_admin.llm import LLMClient, LLMSettings
from news_admin.models import Article

ANALYSIS = {
    'title_zh': '美联储降息25个基点', 'region': 'international', 'topic': 'economy', 'topic_confidence': 0.4,
    'is_opinion': False, 'entities': ['美联储'], 'key_facts': ['美联储宣布降息25个基点'],
    'event_fingerprint': '美联储|降息|利率|美国', 'impact': 5, 'impact_reason': '影响全球市场',
}


def test_embedding_client_batches_and_caches(tmp_path):
    batches = []

    def handler(request):
        body = json.loads(request.content)
        batches.append(body['input'])
        return httpx.Response(200, json={'data': [{'index': i, 'embedding': [float(len(t)), 1.0]} for i, t in enumerate(body['input'])]})

    client = EmbeddingClient('http://e.test/v1', 'k', 'BAAI/bge-m3', cache_dir=tmp_path, batch_size=2, transport=httpx.MockTransport(handler))
    assert client(['a', 'bb', 'ccc']) == [[1.0, 1.0], [2.0, 1.0], [3.0, 1.0]]
    assert batches == [['a', 'bb'], ['ccc']]
    assert client(['bb', 'dddd'])[1] == [4.0, 1.0]
    assert batches[-1] == ['dddd']


def llm(handler, tmp_path):
    return LLMClient(LLMSettings(base_url='http://l.test/v1', api_key='k', model='m'), cache_dir=tmp_path, transport=httpx.MockTransport(handler))


def reply(obj):
    return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(obj, ensure_ascii=False)}}], 'usage': {}})


def test_analyze_applies_low_confidence_topic_rule_and_skips_failures(tmp_path):
    def handler(request):
        body = json.loads(request.content)
        if 'BAD' in body['messages'][1]['content']:
            return reply({'nope': 1})
        return reply(ANALYSIS)

    arts = [Article(id='1', source='s', url='https://x/1', title='Fed cuts'), Article(id='2', source='s', url='https://x/2', title='BAD')]
    result = analyze_articles(llm(handler, tmp_path), arts, min_topic_confidence=0.5)
    assert set(result) == {'1'}
    assert result['1'].topic.value == 'other'


def test_judge_uses_llm_verdict(tmp_path):
    client = llm(lambda r: reply({'same_event': True, 'reason': '同一次议息会议'}), tmp_path)
    a = Article(id='1', source='s', url='https://x/1', title='A')
    b = Article(id='2', source='s', url='https://x/2', title='B')
    assert make_judge(client, {})(a, b) is True
