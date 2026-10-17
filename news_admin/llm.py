"""OpenAI 兼容接口的结构化调用。

- 输出由 Pydantic 模型定义，优先 response_format=json_schema(strict)；
  网关不支持时自动降级为 json_object，并对后续调用记住这个降级。
- 校验失败时把 Pydantic 报错回传给模型重试。
- 以 (模型, Prompt 版本, 输入) 为键缓存结果，同一篇文章不重复花钱。
- 每次调用产出 LLMCall 记录，用于统计 token、耗时、缓存命中和降级。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

T = TypeVar('T', bound=BaseModel)
_FENCE_RE = re.compile(r'^```(?:json)?\s*|\s*```$', re.S)


class LLMError(RuntimeError):
    pass


@dataclass
class LLMSettings:
    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0
    max_retries: int = 2
    temperature: float = 0.0


@dataclass
class LLMCall:
    stage: str
    model: str
    prompt_version: str
    input_hash: str
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0
    cached: bool = False
    schema_mode: str = 'json_schema'
    error: str = ''


@dataclass
class StructuredResult(Generic[T]):
    value: T
    call: LLMCall


def _strict_schema(model: type[BaseModel]) -> dict:
    """转成 OpenAI strict 模式可接受的 Schema：
    内联 $ref（strict 不允许 $ref 旁带 description 等关键字）、
    所有字段 required、additionalProperties=false、去掉 default。"""
    schema = model.model_json_schema()
    defs = schema.pop('$defs', {})

    def visit(node):
        if isinstance(node, list):
            return [visit(value) for value in node]
        if not isinstance(node, dict):
            return node
        if '$ref' in node:
            target = defs[node['$ref'].rsplit('/', 1)[-1]]
            merged = {**visit(target), **{k: v for k, v in node.items() if k != '$ref'}}
            return merged
        node = {key: visit(value) for key, value in node.items() if key not in ('default', 'title')}
        if node.get('type') == 'object' and 'properties' in node:
            node['required'] = list(node['properties'])
            node['additionalProperties'] = False
        return node

    return visit(schema)


def _parse_json(content: str) -> object:
    return json.loads(_FENCE_RE.sub('', content.strip()))


@dataclass
class LLMClient:
    settings: LLMSettings
    cache_dir: Path | None = None
    transport: httpx.BaseTransport | None = None
    calls: list[LLMCall] = field(default_factory=list)
    _json_schema_supported: bool = True

    def __post_init__(self) -> None:
        self._http = httpx.Client(
            base_url=self.settings.base_url.rstrip('/'),
            headers={'Authorization': f'Bearer {self.settings.api_key}'},
            timeout=self.settings.timeout,
            transport=self.transport,
        )
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, key: str) -> Path | None:
        return self.cache_dir / f'{key}.json' if self.cache_dir else None

    def _post(self, body: dict) -> httpx.Response:
        for attempt in range(3):
            try:
                response = self._http.post('/chat/completions', json=body)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return response
        raise AssertionError('unreachable')

    def structured(self, system: str, user: str, output: type[T], *, stage: str, prompt_version: str) -> StructuredResult[T]:
        key = hashlib.sha256(
            json.dumps([self.settings.model, prompt_version, output.__name__, system, user], ensure_ascii=False).encode()
        ).hexdigest()[:32]
        call = LLMCall(stage=stage, model=self.settings.model, prompt_version=prompt_version, input_hash=key)
        cache_path = self._cache_path(key)
        if cache_path and cache_path.exists():
            try:
                value = output.model_validate_json(cache_path.read_text(encoding='utf-8'))
                call.cached = True
                self.calls.append(call)
                return StructuredResult(value, call)
            except ValidationError:
                cache_path.unlink(missing_ok=True)

        messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
        started = time.perf_counter()
        last_error = ''
        for _ in range(self.settings.max_retries + 1):
            call.attempts += 1
            content = self._complete(messages, output, call)
            try:
                value = output.model_validate(_parse_json(content))
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)[:1500]
                messages = [
                    *messages,
                    {'role': 'assistant', 'content': content},
                    {'role': 'user', 'content': f'上面的输出没有通过校验，请修正后只输出符合 Schema 的 JSON。校验错误：\n{last_error}'},
                ]
                continue
            call.elapsed_ms = int((time.perf_counter() - started) * 1000)
            if cache_path:
                cache_path.write_text(value.model_dump_json(), encoding='utf-8')
            self.calls.append(call)
            return StructuredResult(value, call)
        call.elapsed_ms = int((time.perf_counter() - started) * 1000)
        call.error = last_error
        self.calls.append(call)
        raise LLMError(f'{stage}: 输出 {call.attempts} 次仍未通过校验：{last_error[:300]}')

    def _complete(self, messages: list[dict], output: type[BaseModel], call: LLMCall) -> str:
        body: dict = {'model': self.settings.model, 'messages': messages, 'temperature': self.settings.temperature}
        if self._json_schema_supported:
            body['response_format'] = {
                'type': 'json_schema',
                'json_schema': {'name': output.__name__, 'strict': True, 'schema': _strict_schema(output)},
            }
            response = self._post(body)
            if response.status_code == 400 and re.search(r'json_schema|response_format', response.text, re.I):
                self._json_schema_supported = False
            else:
                return self._content(response, call)
        call.schema_mode = 'json_object'
        schema_hint = json.dumps(output.model_json_schema(), ensure_ascii=False)
        body['messages'] = [
            {'role': 'system', 'content': f'{messages[0]["content"]}\n\n只输出一个 JSON 对象，必须符合以下 JSON Schema：\n{schema_hint}'},
            *messages[1:],
        ]
        body['response_format'] = {'type': 'json_object'}
        return self._content(self._post(body), call)

    @staticmethod
    def _content(response: httpx.Response, call: LLMCall) -> str:
        if response.status_code >= 400:
            raise LLMError(f'HTTP {response.status_code}: {response.text[:300]}')
        data = response.json()
        usage = data.get('usage') or {}
        call.prompt_tokens += int(usage.get('prompt_tokens') or 0)
        call.completion_tokens += int(usage.get('completion_tokens') or 0)
        return str(data['choices'][0]['message'].get('content') or '')
