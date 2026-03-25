from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import TIMEZONE
from .db import db_cursor
from .defaults import DEFAULT_PLATFORMS, DEFAULT_SOURCES


def now_iso() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).isoformat()


def row_to_dict(row) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def list_rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db_cursor() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def get_row(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with db_cursor() as conn:
        row = conn.execute(query, params).fetchone()
        return dict(row) if row else None


def seed_defaults() -> None:
    with db_cursor() as conn:
        existing_sources = {str(row['name']): dict(row) for row in conn.execute('SELECT * FROM sources').fetchall()}
        existing_source_names = set(existing_sources)
        for source in DEFAULT_SOURCES:
            source_name = str(source['name'])
            existing = existing_sources.get(source_name)
            if existing is not None:
                if source_name == '路透社' and 'reutersbest.com' in str(existing.get('rss_url', '')):
                    ts = now_iso()
                    conn.execute(
                        '''
                        UPDATE sources
                        SET category = ?, url = ?, rss_url = ?, selectors_json = ?, include_patterns_json = ?, exclude_patterns_json = ?, updated_at = ?
                        WHERE id = ?
                        ''',
                        (
                            source['category'],
                            source['url'],
                            source['rss_url'],
                            json.dumps(source['selectors'], ensure_ascii=False),
                            json.dumps(source['include_patterns'], ensure_ascii=False),
                            json.dumps(source['exclude_patterns'], ensure_ascii=False),
                            ts,
                            existing['id'],
                        ),
                    )
                continue
            ts = now_iso()
            conn.execute(
                '''
                INSERT INTO sources (name, category, url, rss_url, selectors_json, include_patterns_json, exclude_patterns_json, enabled, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, '', ?, ?)
                ''',
                (
                    source['name'],
                    source['category'],
                    source['url'],
                    source['rss_url'],
                    json.dumps(source['selectors'], ensure_ascii=False),
                    json.dumps(source['include_patterns'], ensure_ascii=False),
                    json.dumps(source['exclude_patterns'], ensure_ascii=False),
                    ts,
                    ts,
                ),
            )
        platform_count = conn.execute('SELECT COUNT(*) FROM platforms').fetchone()[0]
        if platform_count == 0:
            for platform in DEFAULT_PLATFORMS:
                ts = now_iso()
                conn.execute(
                    'INSERT INTO platforms (kind, name, config_json, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                    (platform['kind'], platform['name'], json.dumps(platform['config'], ensure_ascii=False), platform['enabled'], ts, ts),
                )


def source_payload(form: dict[str, Any]) -> tuple[Any, ...]:
    ts = now_iso()
    return (
        form['name'].strip(),
        form['category'].strip() or '观察/补充',
        form['url'].strip(),
        form.get('rss_url', '').strip(),
        json.dumps(split_lines(form.get('selectors', 'a')), ensure_ascii=False),
        json.dumps(split_lines(form.get('include_patterns', '')), ensure_ascii=False),
        json.dumps(split_lines(form.get('exclude_patterns', '')), ensure_ascii=False),
        1 if form.get('enabled') else 0,
        form.get('notes', '').strip(),
        ts,
    )


def split_lines(value: str) -> list[str]:
    return [line.strip() for line in (value or '').splitlines() if line.strip()]


def split_contacts(value: str) -> list[str]:
    parts = re.split(r'[,;\n]+', value or '')
    return [part.strip() for part in parts if part.strip()]


def platform_config_payload(kind: str, form: dict[str, Any]) -> dict[str, Any]:
    if kind == 'telegram':
        return {'bot_token': form.get('bot_token', '').strip()}
    if kind == 'email':
        smtp_port_raw = str(form.get('smtp_port', '') or '465').strip()
        try:
            smtp_port = int(smtp_port_raw)
        except ValueError:
            smtp_port = 465
        security_mode = str(form.get('security_mode', '')).strip().lower()
        if security_mode not in {'ssl', 'starttls'}:
            if form.get('use_tls'):
                security_mode = 'starttls'
            else:
                security_mode = 'ssl'
        return {
            'smtp_host': form.get('smtp_host', '').strip(),
            'smtp_port': smtp_port,
            'smtp_username': form.get('smtp_username', '').strip(),
            'smtp_password': form.get('smtp_password', '').strip(),
            'from_email': form.get('from_email', '').strip(),
            'use_tls': security_mode == 'starttls',
            'use_ssl': security_mode == 'ssl',
        }
    return {}


def recipient_target_payload(kind: str, form: dict[str, Any]) -> dict[str, Any]:
    if kind == 'telegram':
        return {
            'chat_id': form.get('chat_id', '').strip(),
            'message_thread_id': form.get('message_thread_id', '').strip(),
        }
    if kind == 'dingtalk':
        return {
            'webhook': form.get('dingtalk_webhook', form.get('webhook', '')).strip(),
            'secret': form.get('dingtalk_secret', form.get('secret', '')).strip(),
            'at_mobiles': split_lines(form.get('at_mobiles', '')),
            'at_user_ids': split_lines(form.get('at_user_ids', '')),
        }
    if kind == 'wechat':
        return {
            'webhook': form.get('wechat_webhook', form.get('webhook', '')).strip(),
            'mentioned_mobile_list': split_lines(form.get('mentioned_mobile_list', '')),
            'mentioned_list': split_lines(form.get('mentioned_list', '')),
        }
    if kind == 'feishu':
        return {
            'webhook': form.get('feishu_webhook', form.get('webhook', '')).strip(),
            'secret': form.get('feishu_secret', form.get('secret', '')).strip(),
        }
    if kind == 'email':
        return {
            'to_emails': split_contacts(form.get('to_emails', '')),
            'cc_emails': split_contacts(form.get('cc_emails', '')),
            'subject': form.get('subject', '').strip(),
        }
    return {}


def recipient_target_issue(kind: str, target: dict[str, Any]) -> str:
    if kind == 'telegram' and not str(target.get('chat_id', '')).strip():
        return '缺少 Telegram User/Group ID'
    if kind in {'dingtalk', 'wechat', 'feishu'} and not str(target.get('webhook', '')).strip():
        return '缺少 Webhook'
    if kind == 'email' and not [str(item).strip() for item in target.get('to_emails', []) if str(item).strip()]:
        return '缺少收件邮箱'
    return ''


def create_source(form: dict[str, Any]) -> None:
    with db_cursor() as conn:
        ts = now_iso()
        payload = source_payload(form)
        conn.execute(
            '''
            INSERT INTO sources (name, category, url, rss_url, selectors_json, include_patterns_json, exclude_patterns_json, enabled, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (*payload[:-1], ts, ts),
        )


def update_source(source_id: int, form: dict[str, Any]) -> None:
    with db_cursor() as conn:
        payload = source_payload(form)
        conn.execute(
            '''
            UPDATE sources
            SET name = ?, category = ?, url = ?, rss_url = ?, selectors_json = ?, include_patterns_json = ?, exclude_patterns_json = ?, enabled = ?, notes = ?, updated_at = ?
            WHERE id = ?
            ''',
            (*payload, source_id),
        )


def set_source_enabled(source_id: int, enabled: bool) -> None:
    with db_cursor() as conn:
        conn.execute(
            'UPDATE sources SET enabled = ?, updated_at = ? WHERE id = ?',
            (1 if enabled else 0, now_iso(), source_id),
        )


def delete_source(source_id: int) -> None:
    with db_cursor() as conn:
        conn.execute('DELETE FROM sources WHERE id = ?', (source_id,))


def list_sources() -> list[dict[str, Any]]:
    rows = list_rows('SELECT * FROM sources ORDER BY enabled DESC, name ASC')
    for row in rows:
        inflate_source(row)
    return rows


def get_source(source_id: int) -> dict[str, Any] | None:
    row = get_row('SELECT * FROM sources WHERE id = ?', (source_id,))
    if row:
        inflate_source(row)
    return row


def inflate_source(row: dict[str, Any]) -> None:
    row['selectors'] = json.loads(row['selectors_json'])
    row['include_patterns'] = json.loads(row['include_patterns_json'])
    row['exclude_patterns'] = json.loads(row['exclude_patterns_json'])


def create_or_update_platform(platform_id: int | None, form: dict[str, Any]) -> None:
    ts = now_iso()
    kind = str(form['kind']).strip()
    config = platform_config_payload(kind, form)
    with db_cursor() as conn:
        if platform_id is None:
            conn.execute(
                'INSERT INTO platforms (kind, name, config_json, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                (kind, form['name'].strip(), json.dumps(config, ensure_ascii=False), 1 if form.get('enabled') else 0, ts, ts),
            )
        else:
            conn.execute(
                'UPDATE platforms SET kind = ?, name = ?, config_json = ?, enabled = ?, updated_at = ? WHERE id = ?',
                (kind, form['name'].strip(), json.dumps(config, ensure_ascii=False), 1 if form.get('enabled') else 0, ts, platform_id),
            )


def list_platforms() -> list[dict[str, Any]]:
    rows = list_rows('SELECT * FROM platforms ORDER BY name ASC')
    for row in rows:
        row['config'] = json.loads(row['config_json'])
    return rows


def get_platform(platform_id: int) -> dict[str, Any] | None:
    row = get_row('SELECT * FROM platforms WHERE id = ?', (platform_id,))
    if row:
        row['config'] = json.loads(row['config_json'])
    return row


def delete_platform(platform_id: int) -> None:
    with db_cursor() as conn:
        conn.execute('DELETE FROM platforms WHERE id = ?', (platform_id,))


def create_or_update_recipient(recipient_id: int | None, form: dict[str, Any]) -> None:
    ts = now_iso()
    platform = get_platform(int(form['platform_id']))
    if not platform:
        raise ValueError('platform not found')
    kind = str(platform['kind'])
    target = recipient_target_payload(kind, form)
    with db_cursor() as conn:
        if recipient_id is None:
            conn.execute(
                'INSERT INTO recipients (platform_id, name, target_json, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                (int(form['platform_id']), form['name'].strip(), json.dumps(target, ensure_ascii=False), 1 if form.get('enabled') else 0, ts, ts),
            )
        else:
            conn.execute(
                'UPDATE recipients SET platform_id = ?, name = ?, target_json = ?, enabled = ?, updated_at = ? WHERE id = ?',
                (int(form['platform_id']), form['name'].strip(), json.dumps(target, ensure_ascii=False), 1 if form.get('enabled') else 0, ts, recipient_id),
            )


def list_recipients() -> list[dict[str, Any]]:
    rows = list_rows(
        '''
        SELECT recipients.*, platforms.kind AS platform_kind, platforms.name AS platform_name
        FROM recipients JOIN platforms ON recipients.platform_id = platforms.id
        ORDER BY recipients.enabled DESC, recipients.name ASC
        '''
    )
    for row in rows:
        row['target'] = json.loads(row['target_json'])
        row['target_issue'] = recipient_target_issue(str(row['platform_kind']), row['target'])
    return rows


def get_recipient(recipient_id: int) -> dict[str, Any] | None:
    row = get_row('SELECT * FROM recipients WHERE id = ?', (recipient_id,))
    if row:
        row['target'] = json.loads(row['target_json'])
    return row


def set_recipient_enabled(recipient_id: int, enabled: bool) -> None:
    with db_cursor() as conn:
        conn.execute(
            'UPDATE recipients SET enabled = ?, updated_at = ? WHERE id = ?',
            (1 if enabled else 0, now_iso(), recipient_id),
        )


def delete_recipient(recipient_id: int) -> None:
    with db_cursor() as conn:
        conn.execute('DELETE FROM recipients WHERE id = ?', (recipient_id,))


def create_or_update_schedule(schedule_id: int | None, form: dict[str, Any]) -> None:
    ts = now_iso()
    with db_cursor() as conn:
        if schedule_id is None:
            conn.execute(
                'INSERT INTO schedules (name, cron_expr, timezone, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)',
                (form['name'].strip(), form['cron_expr'].strip(), form.get('timezone', TIMEZONE).strip(), 1 if form.get('enabled') else 0, ts, ts),
            )
        else:
            conn.execute(
                'UPDATE schedules SET name = ?, cron_expr = ?, timezone = ?, enabled = ?, updated_at = ? WHERE id = ?',
                (form['name'].strip(), form['cron_expr'].strip(), form.get('timezone', TIMEZONE).strip(), 1 if form.get('enabled') else 0, ts, schedule_id),
            )


def list_schedules() -> list[dict[str, Any]]:
    return list_rows('SELECT * FROM schedules ORDER BY name ASC')


def get_schedule(schedule_id: int) -> dict[str, Any] | None:
    return get_row('SELECT * FROM schedules WHERE id = ?', (schedule_id,))


def set_schedule_enabled(schedule_id: int, enabled: bool) -> None:
    with db_cursor() as conn:
        conn.execute(
            'UPDATE schedules SET enabled = ?, updated_at = ? WHERE id = ?',
            (1 if enabled else 0, now_iso(), schedule_id),
        )


def delete_schedule(schedule_id: int) -> None:
    with db_cursor() as conn:
        conn.execute('DELETE FROM schedules WHERE id = ?', (schedule_id,))


def create_run(trigger_kind: str) -> int:
    with db_cursor() as conn:
        cur = conn.execute(
            'INSERT INTO runs (trigger_kind, started_at, status, summary_json, error_text) VALUES (?, ?, ?, ?, ?)',
            (trigger_kind, now_iso(), 'running', '{}', ''),
        )
        if cur.lastrowid is None:
            raise RuntimeError('failed to create run')
        return int(cur.lastrowid)


def finish_run(run_id: int, status: str, summary: dict[str, Any], error_text: str = '') -> None:
    with db_cursor() as conn:
        conn.execute(
            'UPDATE runs SET completed_at = ?, status = ?, summary_json = ?, error_text = ? WHERE id = ?',
            (now_iso(), status, json.dumps(summary, ensure_ascii=False), error_text, run_id),
        )


def mark_running_runs_stale(error_text: str) -> int:
    with db_cursor() as conn:
        rows = conn.execute("SELECT id FROM runs WHERE status = 'running'").fetchall()
        if not rows:
            return 0
        completed_at = now_iso()
        conn.execute(
            "UPDATE runs SET completed_at = ?, status = 'failed', error_text = ? WHERE status = 'running'",
            (completed_at, error_text),
        )
        return len(rows)


def add_run_source_result(run_id: int, source_id: int, status: str, fetched_count: int, kept_count: int, raw_relpath: str, error_text: str = '') -> None:
    with db_cursor() as conn:
        conn.execute(
            'INSERT INTO run_source_results (run_id, source_id, status, fetched_count, kept_count, raw_relpath, error_text) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (run_id, source_id, status, fetched_count, kept_count, raw_relpath, error_text),
        )


def mark_source_health(source_id: int, ok: bool, error_text: str = '') -> None:
    with db_cursor() as conn:
        if ok:
            conn.execute(
                'UPDATE sources SET last_success_at = ?, consecutive_failures = 0, updated_at = ? WHERE id = ?',
                (now_iso(), now_iso(), source_id),
            )
        else:
            conn.execute(
                'UPDATE sources SET last_error_at = ?, consecutive_failures = consecutive_failures + 1, updated_at = ? WHERE id = ?',
                (now_iso(), now_iso(), source_id),
            )


def create_story_group(run_id: int, canonical_key: str, category: str, title: str, score: float, source_count: int, summary_text: str) -> int:
    with db_cursor() as conn:
        cur = conn.execute(
            'INSERT INTO story_groups (run_id, canonical_key, category, title, score, source_count, summary_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (run_id, canonical_key, category, title, score, source_count, summary_text, now_iso()),
        )
        if cur.lastrowid is None:
            raise RuntimeError('failed to create story group')
        return int(cur.lastrowid)


def add_story_evidence(group_id: int, source_id: int, title: str, url: str, snippet: str, rank_order: int) -> None:
    with db_cursor() as conn:
        conn.execute(
            'INSERT INTO story_evidence (group_id, source_id, title, url, snippet, rank_order) VALUES (?, ?, ?, ?, ?, ?)',
            (group_id, source_id, title, url, snippet, rank_order),
        )


def create_publication(run_id: int, title: str, relpath: str, latest_relpath: str) -> int:
    with db_cursor() as conn:
        cur = conn.execute(
            'INSERT INTO publications (run_id, title, relpath, latest_relpath, created_at) VALUES (?, ?, ?, ?, ?)',
            (run_id, title, relpath, latest_relpath, now_iso()),
        )
        if cur.lastrowid is None:
            raise RuntimeError('failed to create publication')
        return int(cur.lastrowid)


def create_delivery(run_id: int, publication_id: int, recipient_id: int, platform_kind: str, idempotency_key: str, request_json: dict[str, Any]) -> int:
    with db_cursor() as conn:
        cur = conn.execute(
            'INSERT OR IGNORE INTO deliveries (run_id, publication_id, recipient_id, platform_kind, status, attempts, request_json, response_json, error_text, idempotency_key, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)',
            (run_id, publication_id, recipient_id, platform_kind, 'pending', json.dumps(request_json, ensure_ascii=False), '{}', '', idempotency_key, now_iso(), now_iso()),
        )
        return int(cur.lastrowid or 0)


def update_delivery(delivery_id: int, status: str, attempts: int, response_json: dict[str, Any], error_text: str = '', next_retry_at: str | None = None) -> None:
    with db_cursor() as conn:
        conn.execute(
            'UPDATE deliveries SET status = ?, attempts = ?, response_json = ?, error_text = ?, next_retry_at = ?, updated_at = ? WHERE id = ?',
            (status, attempts, json.dumps(response_json, ensure_ascii=False), error_text, next_retry_at, now_iso(), delivery_id),
        )


def list_runs(limit: int = 30) -> list[dict[str, Any]]:
    rows = list_rows('SELECT * FROM runs ORDER BY id DESC LIMIT ?', (limit,))
    for row in rows:
        row['summary'] = json.loads(row['summary_json'])
    return rows


def get_run(run_id: int) -> dict[str, Any] | None:
    row = get_row('SELECT * FROM runs WHERE id = ?', (run_id,))
    if row:
        row['summary'] = json.loads(row['summary_json'])
    return row


def list_run_source_results(run_id: int) -> list[dict[str, Any]]:
    return list_rows(
        '''
        SELECT run_source_results.*, sources.name AS source_name
        FROM run_source_results JOIN sources ON run_source_results.source_id = sources.id
        WHERE run_id = ? ORDER BY source_name ASC
        ''',
        (run_id,),
    )


def list_story_groups(run_id: int) -> list[dict[str, Any]]:
    rows = list_rows('SELECT * FROM story_groups WHERE run_id = ? ORDER BY score DESC, id ASC', (run_id,))
    for row in rows:
        row['evidence'] = list_rows(
            '''
            SELECT story_evidence.*, sources.name AS source_name
            FROM story_evidence JOIN sources ON story_evidence.source_id = sources.id
            WHERE group_id = ? ORDER BY rank_order ASC
            ''',
            (row['id'],),
        )
    return rows


def list_publications(limit: int = 30) -> list[dict[str, Any]]:
    rows = list_rows('SELECT publications.*, runs.status AS run_status FROM publications JOIN runs ON publications.run_id = runs.id ORDER BY publications.id DESC LIMIT ?', (limit,))
    for row in rows:
        row['token'] = publication_token(str(row['relpath']))
    return rows


def list_activity_rows(limit: int = 50) -> list[dict[str, Any]]:
    rows = list_rows(
        '''
        SELECT
            runs.id AS run_id,
            runs.trigger_kind,
            runs.status AS run_status,
            runs.started_at,
            runs.completed_at,
            runs.summary_json,
            runs.error_text,
            publications.id AS publication_id,
            publications.title AS publication_title,
            publications.relpath,
            publications.latest_relpath,
            publications.created_at AS publication_created_at
        FROM runs
        LEFT JOIN publications ON publications.run_id = runs.id
        ORDER BY runs.id DESC
        LIMIT ?
        ''',
        (limit,),
    )
    for row in rows:
        row['summary'] = json.loads(row['summary_json']) if row.get('summary_json') else {}
        relpath = str(row.get('relpath') or '')
        row['token'] = publication_token(relpath) if relpath else ''
    return rows


def get_publication(publication_id: int) -> dict[str, Any] | None:
    row = get_row('SELECT * FROM publications WHERE id = ?', (publication_id,))
    if row:
        row['token'] = publication_token(str(row['relpath']))
    return row


def get_publication_by_token(token: str) -> dict[str, Any] | None:
    row = get_row('SELECT * FROM publications WHERE relpath = ? LIMIT 1', (f'publications/{token}.html',))
    if row:
        row['token'] = publication_token(str(row['relpath']))
    return row


def get_latest_publication() -> dict[str, Any] | None:
    row = get_row('SELECT * FROM publications ORDER BY id DESC LIMIT 1')
    if row:
        row['token'] = publication_token(str(row['relpath']))
    return row


def get_first_completed_run_on_date(date_text: str) -> dict[str, Any] | None:
    row = get_row(
        "SELECT * FROM runs WHERE status = 'completed' AND started_at LIKE ? ORDER BY id ASC LIMIT 1",
        (f'{date_text}%',),
    )
    if row:
        row['summary'] = json.loads(row['summary_json']) if row.get('summary_json') else {}
    return row


def publication_token(relpath: str) -> str:
    return Path(relpath).stem


def list_deliveries(run_id: int) -> list[dict[str, Any]]:
    rows = list_rows(
        '''
        SELECT deliveries.*, recipients.name AS recipient_name
        FROM deliveries JOIN recipients ON deliveries.recipient_id = recipients.id
        WHERE run_id = ? ORDER BY deliveries.id DESC
        ''',
        (run_id,),
    )
    for row in rows:
        row['request'] = json.loads(row['request_json']) if row.get('request_json') else {}
        row['response'] = json.loads(row['response_json']) if row.get('response_json') else {}
    return rows


def get_pending_deliveries() -> list[dict[str, Any]]:
    return list_rows(
        '''
        SELECT deliveries.*, recipients.target_json, recipients.name AS recipient_name, recipients.platform_id, platforms.config_json, platforms.kind AS platform_kind_actual
        FROM deliveries
        JOIN recipients ON deliveries.recipient_id = recipients.id
        JOIN platforms ON recipients.platform_id = platforms.id
        WHERE (
            deliveries.status = 'pending'
            OR (
              deliveries.status = 'retry'
              AND deliveries.next_retry_at IS NOT NULL
              AND julianday(deliveries.next_retry_at) <= julianday('now')
            )
          )
          AND recipients.enabled = 1
          AND platforms.enabled = 1
        ORDER BY deliveries.id ASC
        '''
    )


def get_dashboard_stats() -> dict[str, Any]:
    with db_cursor() as conn:
        return {
            'sources': conn.execute('SELECT COUNT(*) FROM sources').fetchone()[0],
            'recipients': conn.execute('SELECT COUNT(*) FROM recipients').fetchone()[0],
            'platforms': conn.execute('SELECT COUNT(*) FROM platforms').fetchone()[0],
            'schedules': conn.execute('SELECT COUNT(*) FROM schedules').fetchone()[0],
            'runs': conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0],
            'publications': conn.execute('SELECT COUNT(*) FROM publications').fetchone()[0],
            'healthy_sources': conn.execute('SELECT COUNT(*) FROM sources WHERE consecutive_failures = 0').fetchone()[0],
        }
