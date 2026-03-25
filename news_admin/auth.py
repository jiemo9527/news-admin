from __future__ import annotations

import json
import os
import secrets
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from .config import Paths
from .repository import now_iso

AUTH_PATH = Paths.data_dir / 'admin_auth.json'
BOOTSTRAP_PATH = Paths.data_dir / 'admin_bootstrap.txt'


def _write_json(payload: dict[str, Any]) -> None:
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')


def ensure_admin_auth() -> dict[str, Any]:
    env_username = os.getenv('NEWS_ADMIN_ADMIN_USERNAME', '').strip()
    env_password = os.getenv('NEWS_ADMIN_ADMIN_PASSWORD', '').strip()
    env_hash = os.getenv('NEWS_ADMIN_ADMIN_PASSWORD_HASH', '').strip()
    if env_username and (env_password or env_hash):
        password_hash = env_hash or generate_password_hash(env_password)
        return {'username': env_username, 'password_hash': password_hash, 'source': 'env'}
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    if AUTH_PATH.exists():
        payload = json.loads(AUTH_PATH.read_text(encoding='utf-8'))
        payload['source'] = 'file'
        return payload
    bootstrap_password = secrets.token_urlsafe(14)
    payload = {
        'username': 'admin',
        'password_hash': generate_password_hash(bootstrap_password),
        'created_at': now_iso(),
        'updated_at': now_iso(),
    }
    _write_json(payload)
    BOOTSTRAP_PATH.write_text(
        f'username=admin\npassword={bootstrap_password}\ncreated_at={payload["created_at"]}\n',
        encoding='utf-8',
    )
    payload['bootstrap_password'] = bootstrap_password
    payload['source'] = 'bootstrap'
    return payload


def get_admin_auth() -> dict[str, Any]:
    return ensure_admin_auth()


def verify_admin_password(password: str) -> bool:
    auth = get_admin_auth()
    return check_password_hash(str(auth['password_hash']), password)


def update_admin_password(new_password: str, username: str | None = None) -> dict[str, Any]:
    current = get_admin_auth()
    payload = {
        'username': (username or str(current['username'])).strip() or str(current['username']),
        'password_hash': generate_password_hash(new_password),
        'created_at': current.get('created_at', now_iso()),
        'updated_at': now_iso(),
    }
    _write_json(payload)
    if BOOTSTRAP_PATH.exists():
        BOOTSTRAP_PATH.unlink()
    payload['source'] = 'file'
    return payload
