"""推送渠道：Telegram / 钉钉 / 飞书 / 企业微信 / 邮件。长消息按段落切分。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import smtplib
import time
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

import httpx

CHANNEL_KINDS: dict[str, dict[str, Any]] = {
    'telegram': {
        'label': 'Telegram',
        'limit': 3800,
        'fields': {'bot_token': 'Bot Token', 'chat_id': 'Chat ID', 'thread_id': 'Topic ID（可选）'},
        'required': ['bot_token', 'chat_id'],
    },
    'dingtalk': {
        'label': '钉钉机器人',
        'limit': 3600,
        'fields': {'webhook': 'Webhook', 'secret': '加签密钥（可选）'},
        'required': ['webhook'],
    },
    'feishu': {
        'label': '飞书机器人',
        'limit': 3800,
        'fields': {'webhook': 'Webhook', 'secret': '签名密钥（可选）'},
        'required': ['webhook'],
    },
    'wecom': {
        'label': '企业微信机器人',
        'limit': 1900,
        'fields': {'webhook': 'Webhook'},
        'required': ['webhook'],
    },
    'email': {
        'label': '邮件',
        'limit': 0,
        'fields': {
            'smtp_host': 'SMTP 主机',
            'smtp_port': '端口（465/587）',
            'username': '用户名',
            'password': '密码',
            'from_addr': '发件人',
            'to_addrs': '收件人（逗号分隔）',
        },
        'required': ['smtp_host', 'from_addr', 'to_addrs'],
    },
}


def split_chunks(text: str, limit: int) -> list[str]:
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ''
    for paragraph in text.split('\n\n'):
        while len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ''
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        candidate = f'{current}\n\n{paragraph}' if current else paragraph
        if len(candidate) > limit:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _check(response: httpx.Response, ok: Callable[[dict[str, Any]], bool]) -> None:
    response.raise_for_status()
    data = response.json()
    if not ok(data):
        message = data.get('errmsg') or data.get('msg') or data.get('description') or data
        raise RuntimeError(str(message)[:300])


def _sign(secret: str, millis: bool) -> tuple[str, str]:
    """钉钉：HMAC(key=secret, msg=ts\\nsecret)，毫秒时间戳；飞书：HMAC(key=ts\\nsecret, msg=空)，秒级时间戳。"""
    timestamp = str(int(time.time() * (1000 if millis else 1)))
    string = f'{timestamp}\n{secret}'.encode()
    if millis:
        digest = hmac.new(secret.encode(), string, hashlib.sha256).digest()
    else:
        digest = hmac.new(string, b'', hashlib.sha256).digest()
    return timestamp, base64.b64encode(digest).decode()


def send(kind: str, config: dict[str, Any], text: str, subject: str = '新闻简报', transport: httpx.BaseTransport | None = None) -> int:
    """发送一条简报，返回实际发送的分段数。失败抛异常，由调用方记录。"""
    spec = CHANNEL_KINDS.get(kind)
    if not spec:
        raise ValueError(f'不支持的渠道：{kind}')
    missing = [name for name in spec['required'] if not str(config.get(name, '')).strip()]
    if missing:
        raise ValueError('缺少配置：' + '、'.join(missing))
    if kind == 'email':
        _send_email(config, subject, text)
        return 1
    first_line, separator, rest = text.partition('\n')
    link = first_line if first_line.startswith(('https://', 'http://')) else ''
    limit = spec['limit']
    if link and len(link) + 32 >= limit:
        raise ValueError('简报链接过长，无法分段发送')
    chunks = split_chunks(rest.lstrip('\n'), limit - len(link) - 32) if link else split_chunks(text, limit)
    with httpx.Client(timeout=20, transport=transport) as client:
        for index, chunk in enumerate(chunks, 1):
            body = f'[{index}/{len(chunks)}]\n{chunk}' if len(chunks) > 1 else chunk
            if link:
                body = link + '\n\n' + body
            if kind == 'telegram':
                payload: dict[str, Any] = {'chat_id': config['chat_id'], 'text': body, 'disable_web_page_preview': True}
                if config.get('thread_id'):
                    payload['message_thread_id'] = int(config['thread_id'])
                url = f"https://api.telegram.org/bot{config['bot_token']}/sendMessage"
                _check(client.post(url, json=payload), lambda d: bool(d.get('ok')))
            elif kind == 'dingtalk':
                params: dict[str, str] = {}
                if config.get('secret'):
                    params['timestamp'], params['sign'] = _sign(config['secret'], millis=True)
                payload = {'msgtype': 'text', 'text': {'content': body}}
                _check(client.post(config['webhook'], params=params, json=payload), lambda d: d.get('errcode') == 0)
            elif kind == 'feishu':
                payload = {'msg_type': 'text', 'content': {'text': body}}
                if config.get('secret'):
                    payload['timestamp'], payload['sign'] = _sign(config['secret'], millis=False)
                _check(client.post(config['webhook'], json=payload), lambda d: d.get('code', d.get('StatusCode', 0)) == 0)
            elif kind == 'wecom':
                payload = {'msgtype': 'text', 'text': {'content': body}}
                _check(client.post(config['webhook'], json=payload), lambda d: d.get('errcode') == 0)
    return len(chunks)


def _send_email(config: dict[str, Any], subject: str, text: str) -> None:
    recipients = [addr.strip() for addr in str(config['to_addrs']).split(',') if addr.strip()]
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = config['from_addr']
    message['To'] = ', '.join(recipients)
    message.set_content(text)
    port = int(config.get('smtp_port') or 465)
    if port == 465:
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(config['smtp_host'], port, timeout=30)
    else:
        smtp = smtplib.SMTP(config['smtp_host'], port, timeout=30)
        smtp.starttls()
    with smtp:
        if config.get('username'):
            smtp.login(config['username'], config.get('password', ''))
        smtp.send_message(message, to_addrs=recipients)
