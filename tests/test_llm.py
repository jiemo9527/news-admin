import json

import httpx
import pytest
from pydantic import BaseModel

from news_admin.llm import LLMClient, LLMError, LLMSettings


class Out(BaseModel):
    answer: int


def chat_reply(content, usage=None):
    return {'choices': [{'message': {'content': content}}], 'usage': usage or {'prompt_tokens': 10, 'completion_tokens': 2}}


def make_client(handler, tmp_path, **kw):
    settings = LLMSettings(base_url='http://llm.test/v1', api_key='k', model='m', **kw)
    return LLMClient(settings, cache_dir=tmp_path, transport=httpx.MockTransport(handler))


def test_structured_call_sends_strict_json_schema_and_parses_result(tmp_path):
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=chat_reply('{"answer": 42}'))

    result = make_client(handler, tmp_path).structured('sys', 'user', Out, stage='t', prompt_version='v1')
    assert result.value == Out(answer=42)
    assert seen['response_format']['type'] == 'json_schema'
    assert seen['response_format']['json_schema']['strict'] is True
    assert result.call.prompt_tokens == 10


def test_validation_error_is_fed_back_to_model_and_retried(tmp_path):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=chat_reply('{"answer": "x"}' if len(bodies) == 1 else '{"answer": 1}'))

    result = make_client(handler, tmp_path).structured('sys', 'user', Out, stage='t', prompt_version='v1')
    assert result.value.answer == 1
    assert result.call.attempts == 2
    feedback = bodies[1]['messages'][-1]['content']
    assert 'answer' in feedback and '校验' in feedback


def test_raises_after_exhausting_retries(tmp_path):
    client = make_client(lambda r: httpx.Response(200, json=chat_reply('not json')), tmp_path, max_retries=1)
    with pytest.raises(LLMError):
        client.structured('sys', 'user', Out, stage='t', prompt_version='v1')


def test_falls_back_to_json_object_when_gateway_rejects_json_schema(tmp_path):
    formats = []

    def handler(request):
        body = json.loads(request.content)
        formats.append(body['response_format']['type'])
        if body['response_format']['type'] == 'json_schema':
            return httpx.Response(400, json={'error': {'message': 'response_format json_schema not supported'}})
        return httpx.Response(200, json=chat_reply('```json\n{"answer": 7}\n```'))

    client = make_client(handler, tmp_path)
    assert client.structured('sys', 'user', Out, stage='t', prompt_version='v1').value.answer == 7
    assert client.structured('sys', 'user2', Out, stage='t', prompt_version='v1').value.answer == 7
    assert formats == ['json_schema', 'json_object', 'json_object']


def test_identical_requests_hit_cache(tmp_path):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=chat_reply('{"answer": 5}'))

    client = make_client(handler, tmp_path)
    client.structured('sys', 'same', Out, stage='t', prompt_version='v1')
    second = client.structured('sys', 'same', Out, stage='t', prompt_version='v1')
    assert len(calls) == 1 and second.call.cached is True
    client.structured('sys', 'same', Out, stage='t', prompt_version='v2')
    assert len(calls) == 2


def test_strict_schema_inlines_refs_and_requires_all_fields():
    from news_admin.llm import _strict_schema
    from news_admin.models import ArticleAnalysis, EventSummary

    for model in (ArticleAnalysis, EventSummary):
        schema = _strict_schema(model)
        text = json.dumps(schema)
        assert '$ref' not in text and '$defs' not in text

        def walk(node):
            if isinstance(node, dict):
                if node.get('type') == 'object':
                    assert node['additionalProperties'] is False
                    assert set(node['required']) == set(node['properties'])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(schema)
    region = _strict_schema(ArticleAnalysis)['properties']['region']
    assert region['enum'] and 'description' in region
