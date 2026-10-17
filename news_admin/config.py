"""配置：全部来自环境变量 / .env，敏感信息不入库、不入仓库。"""

from __future__ import annotations

import re
from pathlib import Path
import os

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='NEWS_', env_file=BASE_DIR / '.env', env_file_encoding='utf-8', extra='ignore')

    data_dir: Path = BASE_DIR / 'data'
    public_base_url: str = ''

    llm_base_url: str = 'https://api.openai.com/v1'
    llm_api_key: str = ''
    llm_model: str = 'gpt-5.6-luna'
    llm_concurrency: int = 4

    embedding_base_url: str = 'https://api.siliconflow.cn/v1'
    embedding_api_key: str = ''
    embedding_model: str = 'BAAI/bge-m3'

    # 向量相似度阈值：≥high 直接合并；[low, high) 交给 LLM 判定；<low 不合并。
    # 留空时按向量模型取默认值（bge-m3：0.86/0.72；无向量服务的字符哈希降级：0.75/0.40）。
    cluster_high: float | None = None
    cluster_low: float | None = None
    cluster_window_hours: float = 48
    min_topic_confidence: float = 0.5
    # 价值过滤：影响力低于该值的文章不进入简报；drop_opinion=true 时评论文章全部不收录
    min_impact: int = 2
    drop_opinion: bool = False
    # 每期简报最多收录的事件数（只有这些会调用模型写摘要）；其余事件仍全部入库，首页汇总可见
    max_events: int = 50
    # 每次抓取的时间窗：today_yesterday（昨天 0 点至今，北京时间）或 24h；后台可改
    fetch_window: str = 'today_yesterday'
    # 历史保留天数：超过的运行、事件、缩略图会被清理
    retention_days: int = 90

    # 部署在 Nginx 等反向代理之后时设为 true，才会采用 X-Forwarded-For 中的客户端地址
    trust_proxy: bool = False

    admin_username: str = 'admin'
    admin_password: str = ''
    secret_key: str = ''

    # 后台「系统设置 → 模型与向量」保存时写回的 .env 文件
    env_path: Path = BASE_DIR / '.env'

    @property
    def db_path(self) -> Path:
        return self.data_dir / 'news.db'

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / 'cache'


def get_settings() -> Settings:
    path = Path(os.environ.get('NEWS_ENV_PATH', str(BASE_DIR / '.env')))
    return Settings(_env_file=path, env_path=path)


_ENV_LINE_RE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=')


def _quote(value: str) -> str:
    if value == '' or re.fullmatch(r'[A-Za-z0-9_./:@+\-,]+', value):
        return value
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def update_env_file(path: Path, values: dict[str, str | None]) -> None:
    """就地更新 .env：保留注释、空行和其他变量；值为 None 表示删除该行；新变量追加到末尾。"""
    for key, value in values.items():
        if value is not None and ('\n' in value or '\r' in value):
            raise ValueError(f'{key} 含换行')
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    pending = dict(values)
    out: list[str] = []
    for line in lines:
        match = _ENV_LINE_RE.match(line)
        key = match.group(1) if match else None
        if key in pending:
            value = pending.pop(key)
            if value is not None:
                out.append(f'{key}={_quote(value)}')
            continue
        out.append(line)
    for key, value in pending.items():
        if value is not None:
            out.append(f'{key}={_quote(value)}')
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text('\n'.join(out) + '\n', encoding='utf-8')
    tmp.replace(path)
