import json

import httpx
import pytest

from news_admin.push import CHANNEL_KINDS, send, split_chunks


def test_split_chunks_prefers_paragraph_boundaries():
    text = '\n\n'.join(['a' * 40] * 5)
    chunks = split_chunks(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert ''.join(chunks).replace('\n', '') == 'a' * 200


def test_telegram_sends_each_chunk_with_part_numbers():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={'ok': True})

    send('telegram', {'bot_token': 't', 'chat_id': '1'}, 'x' * 5000, transport=httpx.MockTransport(handler))
    assert len(sent) == 2 and sent[0]['text'].startswith('[1/2]') and sent[0]['chat_id'] == '1'


def test_dingtalk_signs_request_and_raises_on_errcode():
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json={'errcode': 310000, 'errmsg': 'sign not match'})

    with pytest.raises(RuntimeError, match='sign not match'):
        send('dingtalk', {'webhook': 'https://oapi.dingtalk.com/robot/send?access_token=a', 'secret': 's'}, 'hi', transport=httpx.MockTransport(handler))
    assert 'sign' in seen[0].params and 'timestamp' in seen[0].params


def test_missing_required_field_is_reported():
    with pytest.raises(ValueError, match='chat_id'):
        send('telegram', {'bot_token': 't'}, 'hi')


def test_every_channel_kind_declares_fields():
    assert set(CHANNEL_KINDS) == {'telegram', 'dingtalk', 'feishu', 'wecom', 'email'}
    assert all(spec['fields'] for spec in CHANNEL_KINDS.values())
