from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / 'data'
DB_PATH = DATA_DIR / 'news_admin.db'
ARTIFACTS_DIR = DATA_DIR / 'artifacts'
PUBLICATIONS_DIR = DATA_DIR / 'publications'
EXPORTS_DIR = DATA_DIR / 'exports'
THUMBNAILS_DIR = DATA_DIR / 'thumbnails'
LOCK_PATH = DATA_DIR / 'run.lock'
SECRET_KEY_PATH = DATA_DIR / 'secret_key.txt'
TIMEZONE = 'Asia/Shanghai'
RETENTION_DAYS = 7
PUBLIC_WEB_BASE_URL = os.getenv('NEWS_ADMIN_PUBLIC_BASE_URL', '').strip().rstrip('/')


def load_secret_key() -> str:
    env_value = os.getenv('NEWS_ADMIN_SECRET_KEY', '').strip()
    if env_value:
        return env_value
    SECRET_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_KEY_PATH.exists():
        value = SECRET_KEY_PATH.read_text(encoding='utf-8').strip()
        if value:
            return value
    value = secrets.token_urlsafe(32)
    SECRET_KEY_PATH.write_text(value, encoding='utf-8')
    return value


SECRET_KEY = load_secret_key()


@dataclass(frozen=True)
class Paths:
    base_dir: Path = BASE_DIR
    data_dir: Path = DATA_DIR
    db_path: Path = DB_PATH
    artifacts_dir: Path = ARTIFACTS_DIR
    publications_dir: Path = PUBLICATIONS_DIR
    exports_dir: Path = EXPORTS_DIR
    thumbnails_dir: Path = THUMBNAILS_DIR
    lock_path: Path = LOCK_PATH
    secret_key_path: Path = SECRET_KEY_PATH
