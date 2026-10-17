"""OpenAI 兼容 /embeddings 客户端（默认 SiliconFlow BAAI/bge-m3），分批 + 本地缓存。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx


class EmbeddingClient:
    def __init__(self, base_url: str, api_key: str, model: str, cache_dir: Path | None = None, batch_size: int = 32, transport: httpx.BaseTransport | None = None):
        self.model = model
        self.batch_size = batch_size
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self._http = httpx.Client(base_url=base_url.rstrip('/'), headers={'Authorization': f'Bearer {api_key}'}, timeout=60, transport=transport)
        self.requests = 0

    def _key(self, text: str) -> str:
        return hashlib.sha256(f'{self.model}\n{text}'.encode()).hexdigest()[:32]

    def _load(self, text: str) -> list[float] | None:
        if not self.cache_dir:
            return None
        path = self.cache_dir / f'{self._key(text)}.json'
        return json.loads(path.read_text()) if path.exists() else None

    def _store(self, text: str, vector: list[float]) -> None:
        if self.cache_dir:
            (self.cache_dir / f'{self._key(text)}.json').write_text(json.dumps(vector))

    def __call__(self, texts: list[str]) -> list[list[float]]:
        result: list[list[float] | None] = [self._load(t) for t in texts]
        pending = [i for i, v in enumerate(result) if v is None]
        for start in range(0, len(pending), self.batch_size):
            chunk = pending[start:start + self.batch_size]
            response = self._http.post('/embeddings', json={'model': self.model, 'input': [texts[i] for i in chunk]})
            response.raise_for_status()
            self.requests += 1
            for row in response.json()['data']:
                index = chunk[row['index']]
                result[index] = [float(x) for x in row['embedding']]
                self._store(texts[index], result[index])
        return [v for v in result if v is not None]


class HashingEmbedder:
    """未配置向量服务时的降级方案：字符 2-gram 特征哈希向量。

    只能识别同语言、措辞相近的报道，跨语言（中英文报道同一事件）无法聚到一起；
    此时更多合并依赖 LLM 判定，聚类质量会下降，后台会显示当前使用的是降级向量。
    """

    model = 'hashing-2gram'

    def __init__(self, dims: int = 1024):
        self.dims = dims

    def __call__(self, texts: list[str]) -> list[list[float]]:
        import re
        import zlib

        vectors = []
        for text in texts:
            tokens = re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]', text.lower())
            grams = [''.join(tokens[i:i + 2]) for i in range(len(tokens) - 1)] or tokens
            vector = [0.0] * self.dims
            for gram in grams:
                vector[zlib.crc32(gram.encode()) % self.dims] += 1.0
            vectors.append(vector)
        return vectors
