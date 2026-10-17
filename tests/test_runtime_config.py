import httpx

from news_admin.config import Settings, update_env_file
from news_admin.runtime import build_runtime


def test_update_env_file_preserves_comments_and_quotes_values(tmp_path):
    path = tmp_path / '.env'
    path.write_text('# comment\nNEWS_LLM_MODEL=old\n\nFOO=bar\n', encoding='utf-8')
    update_env_file(path, {'NEWS_LLM_MODEL': 'new model', 'NEWS_CLUSTER_LOW': None, 'NEWS_MAX_EVENTS': '50'})
    text = path.read_text(encoding='utf-8')
    assert text.startswith('# comment\nNEWS_LLM_MODEL="new model"\n\nFOO=bar\n')
    assert text.rstrip().endswith('NEWS_MAX_EVENTS=50')
    assert Settings(_env_file=path).llm_model == 'new model'


def test_llm_and_embedding_connection_tests(tmp_path):
    def handler(request):
        if request.url.path.endswith('/chat/completions'):
            return httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}], 'usage': {}})
        if request.url.path.endswith('/embeddings'):
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [0.1] * 1024}]})
        return httpx.Response(404)

    rt = build_runtime(Settings(data_dir=tmp_path, llm_api_key='k', embedding_api_key='e', env_path=tmp_path / '.env', _env_file=None))
    rt.transport = httpx.MockTransport(handler)
    ok, message = rt.test_llm()
    assert ok and 'gpt-5.6-luna' in message
    ok, message = rt.test_embedding()
    assert ok and '1024' in message
