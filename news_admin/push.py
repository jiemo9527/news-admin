from __future__ import annotations

import base64
import hashlib
import hmac
import json
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from typing import Any

import requests


TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_SAFE_CHUNK = 3800
DINGTALK_TEXT_LIMIT = 4000
DINGTALK_SAFE_CHUNK = 3600


def split_text_chunks(text: str, limit: int) -> list[str]:
    normalized = str(text or '').replace('\r\n', '\n')
    if not normalized:
        return ['']
    chunks: list[str] = []
    remaining = normalized
    while len(remaining) > limit:
        split_at = remaining.rfind('\n', 0, limit + 1)
        if split_at < max(1, limit // 2):
            split_at = remaining.rfind(' ', 0, limit + 1)
        if split_at < max(1, limit // 2):
            split_at = limit
        chunk = remaining[:split_at].strip()
        if not chunk:
            chunk = remaining[:limit].strip()
            split_at = len(chunk)
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip('\n ')
    if remaining:
        chunks.append(remaining.strip())
    return chunks or ['']


def telegram_send(bot_token: str, target: dict[str, Any], text: str) -> dict[str, Any]:
    if not bot_token:
        raise ValueError('Telegram bot token is empty')
    if not target.get('chat_id'):
        raise ValueError('Telegram chat_id is empty')
    chunks = split_text_chunks(text, TELEGRAM_SAFE_CHUNK)
    responses: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks, start=1):
        message_text = chunk if len(chunks) == 1 else f'[{idx}/{len(chunks)}]\n{chunk}'
        if len(message_text) > TELEGRAM_TEXT_LIMIT:
            raise ValueError('Telegram message chunk exceeded size limit after splitting')
        payload = {'chat_id': target['chat_id'], 'text': message_text}
        if target.get('message_thread_id'):
            payload['message_thread_id'] = int(target['message_thread_id'])
        response = requests.post(f'https://api.telegram.org/bot{bot_token}/sendMessage', json=payload, timeout=25)
        response.raise_for_status()
        responses.append(response.json())
    return {'ok': True, 'parts': len(responses), 'messages': responses}


def dingtalk_sign(secret: str) -> tuple[str, str]:
    timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    string_to_sign = f'{timestamp}\n{secret}'.encode('utf-8')
    sign = base64.b64encode(hmac.new(secret.encode('utf-8'), string_to_sign, hashlib.sha256).digest()).decode('utf-8')
    return timestamp, sign


def dingtalk_send(target: dict[str, Any], text: str) -> dict[str, Any]:
    webhook = target.get('webhook', '')
    if not webhook:
        raise ValueError('DingTalk webhook is empty')
    params = {}
    if target.get('secret'):
        timestamp, sign = dingtalk_sign(target['secret'])
        params = {'timestamp': timestamp, 'sign': sign}
    chunks = split_text_chunks(text, DINGTALK_SAFE_CHUNK)
    responses: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks, start=1):
        message_text = chunk if len(chunks) == 1 else f'[{idx}/{len(chunks)}]\n{chunk}'
        if len(message_text) > DINGTALK_TEXT_LIMIT:
            raise ValueError('DingTalk message chunk exceeded size limit after splitting')
        payload = {
            'msgtype': 'text',
            'text': {'content': message_text},
            'at': {
                'isAtAll': False,
                'atMobiles': target.get('at_mobiles', []),
                'atUserIds': target.get('at_user_ids', []),
            },
        }
        response = requests.post(webhook, params=params, json=payload, timeout=25)
        response.raise_for_status()
        responses.append(response.json())
    return {'ok': True, 'parts': len(responses), 'messages': responses}


def feishu_sign(secret: str) -> tuple[str, str]:
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    key = f'{timestamp}\n{secret}'.encode('utf-8')
    sign = base64.b64encode(hmac.new(key, b'', hashlib.sha256).digest()).decode('utf-8')
    return timestamp, sign


def feishu_send(target: dict[str, Any], text: str) -> dict[str, Any]:
    webhook = target.get('webhook', '')
    if not webhook:
        raise ValueError('Feishu webhook is empty')
    payload: dict[str, Any] = {'msg_type': 'text', 'content': {'text': text[:4000]}}
    if target.get('secret'):
        timestamp, sign = feishu_sign(target['secret'])
        payload['timestamp'] = timestamp
        payload['sign'] = sign
    response = requests.post(webhook, json=payload, timeout=25)
    response.raise_for_status()
    return response.json()


def wechat_send(target: dict[str, Any], text: str) -> dict[str, Any]:
    webhook = target.get('webhook', '')
    if not webhook:
        raise ValueError('WeChat webhook is empty')
    payload = {
        'msgtype': 'text',
        'text': {
            'content': text[:4000],
            'mentioned_list': target.get('mentioned_list', []),
            'mentioned_mobile_list': target.get('mentioned_mobile_list', []),
        },
    }
    response = requests.post(webhook, json=payload, timeout=25)
    response.raise_for_status()
    return response.json()


def email_send(platform_config: dict[str, Any], target: dict[str, Any], text: str) -> dict[str, Any]:
    smtp_host = str(platform_config.get('smtp_host', '')).strip()
    smtp_port = int(platform_config.get('smtp_port', 465) or 465)
    smtp_username = str(platform_config.get('smtp_username', '')).strip()
    smtp_password = str(platform_config.get('smtp_password', '')).strip()
    from_email = str(platform_config.get('from_email', '')).strip() or smtp_username
    to_emails = [str(item).strip() for item in target.get('to_emails', []) if str(item).strip()]
    cc_emails = [str(item).strip() for item in target.get('cc_emails', []) if str(item).strip()]
    subject = str(target.get('subject', '')).strip() or '每日新闻聚合简报'
    use_ssl = bool(platform_config.get('use_ssl'))
    use_tls = bool(platform_config.get('use_tls'))
    if not smtp_host:
        raise ValueError('Email smtp_host is empty')
    if not from_email:
        raise ValueError('Email from_email is empty')
    if not to_emails:
        raise ValueError('Email recipients are empty')
    message = EmailMessage()
    message['Subject'] = subject[:200]
    message['From'] = from_email
    message['To'] = ', '.join(to_emails)
    if cc_emails:
        message['Cc'] = ', '.join(cc_emails)
    message.set_content(text)
    recipients = to_emails + cc_emails
    if use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=25) as client:
            if smtp_username:
                client.login(smtp_username, smtp_password)
            client.send_message(message, to_addrs=recipients)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=25) as client:
            if use_tls:
                client.starttls()
            if smtp_username:
                client.login(smtp_username, smtp_password)
            client.send_message(message, to_addrs=recipients)
    return {'ok': True, 'sent_to': recipients, 'subject': message['Subject']}


def send_message(platform_kind: str, platform_config: dict[str, Any], target: dict[str, Any], text: str) -> dict[str, Any]:
    if platform_kind == 'telegram':
        return telegram_send(platform_config.get('bot_token', ''), target, text)
    if platform_kind == 'wechat':
        return wechat_send(target, text)
    if platform_kind == 'dingtalk':
        return dingtalk_send(target, text)
    if platform_kind == 'feishu':
        return feishu_send(target, text)
    if platform_kind == 'email':
        return email_send(platform_config, target, text)
    raise ValueError(f'Unsupported platform kind: {platform_kind}')


def next_retry(attempts: int) -> str:
    delay = min(60, 2 ** max(1, attempts))
    return (datetime.now(timezone.utc) + timedelta(minutes=delay)).isoformat()
