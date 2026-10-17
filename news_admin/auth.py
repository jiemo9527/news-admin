"""账号：PBKDF2 密码哈希 + 两种角色。

- admin  管理员：全部功能
- viewer 只读用户：可浏览新闻和后台各页面，不能运行、修改、删除任何东西
游客（未登录）不能访问任何页面。
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets

ROLES = {'admin': '管理员', 'viewer': '只读用户'}
MIN_PASSWORD = 8
USERNAME_RE = re.compile(r'^[A-Za-z0-9_.\-\u4e00-\u9fff]{2,32}$')


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 200_000).hex()
    return f'{salt}${digest}'


def verify_password(password: str, stored: str) -> bool:
    if not stored or '$' not in stored:
        return False
    return hmac.compare_digest(hash_password(password, stored.split('$', 1)[0]), stored)


def validate_username(name: str) -> str | None:
    return None if USERNAME_RE.match(name or '') else '用户名为 2-32 位字母、数字、中文或 _ . -'


def validate_password(password: str) -> str | None:
    return None if len(password or '') >= MIN_PASSWORD else f'密码至少 {MIN_PASSWORD} 位'


# 去掉容易看错的字符：0/O/o、1/l/I/i、5/S/s、2/Z/z、8/B、u/v/V
AMBIGUOUS = '0Oo1lIi5Ss2Zz8BuvV'
_UPPER = ''.join(c for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' if c not in AMBIGUOUS)
_LOWER = ''.join(c for c in 'abcdefghijklmnopqrstuvwxyz' if c not in AMBIGUOUS)
_DIGITS = ''.join(c for c in '0123456789' if c not in AMBIGUOUS)
_ALPHABET = _UPPER + _LOWER + _DIGITS


def generate_password(groups: int = 4, size: int = 5) -> str:
    """强密码：4 组 × 5 位，用 - 分隔（约 115 位熵），不含易混淆字符，大小写字母和数字都有。"""
    while True:
        chars = [secrets.choice(_ALPHABET) for _ in range(groups * size)]
        value = '-'.join(''.join(chars[i:i + size]) for i in range(0, len(chars), size))
        if any(c in _UPPER for c in value) and any(c in _LOWER for c in value) and any(c in _DIGITS for c in value):
            return value


def generate_username(prefix: str = 'user') -> str:
    return prefix + ''.join(secrets.choice(_LOWER + _DIGITS) for _ in range(4))
