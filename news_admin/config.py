"""配置：全部来自环境变量 / .env，敏感信息不入库、不入仓库。"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='NEWS_', env_file=BASE_DIR / '.env', env_file_encoding='utf-8', extra='ignore')

    data_dir: Path = BASE_DIR / 'data'
    timezone: str = 'Asia/Shanghai'
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
    max_events: int = 30

    admin_username: str = 'admin'
    admin_password: str = ''
    secret_key: str = ''

    @property
    def db_path(self) -> Path:
        return self.data_dir / 'news.db'

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / 'cache'


def get_settings() -> Settings:
    return Settings()
