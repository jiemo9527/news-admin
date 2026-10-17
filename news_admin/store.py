"""SQLite 持久化。事件以 JSON 快照保存，便于复现与评测。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .fetch import Source
from .llm import LLMCall
from .models import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    feed_url TEXT NOT NULL UNIQUE,
    weight REAL NOT NULL DEFAULT 1.0,
    max_items INTEGER NOT NULL DEFAULT 20,
    fetch_body INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_ok_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS source_results (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL,
    article_count INTEGER NOT NULL,
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT NOT NULL,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    score REAL NOT NULL,
    headline TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    degraded INTEGER NOT NULL DEFAULT 0,
    is_update INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, id)
);
CREATE TABLE IF NOT EXISTS event_urls (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    url TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_urls_url ON event_urls(url);
CREATE TABLE IF NOT EXISTS llm_calls (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    elapsed_ms INTEGER NOT NULL,
    cached INTEGER NOT NULL,
    schema_mode TEXT NOT NULL,
    error TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._conn() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    # ---------- sources ----------
    def seed_sources(self, sources: list[dict]) -> None:
        with self._conn() as conn:
            for s in sources:
                conn.execute(
                    'INSERT OR IGNORE INTO sources(name, feed_url, weight) VALUES (?, ?, ?)',
                    (s['name'], s['feed_url'], s.get('weight', 1.0)),
                )

    def list_sources(self, enabled_only: bool = False) -> list[Source]:
        sql = 'SELECT * FROM sources' + (' WHERE enabled = 1' if enabled_only else '') + ' ORDER BY id'
        return [
            Source(
                id=r['id'], name=r['name'], feed_url=r['feed_url'], weight=r['weight'],
                max_items=r['max_items'], fetch_body=bool(r['fetch_body']), enabled=bool(r['enabled']),
            )
            for r in self._rows(sql)
        ]

    def source_health(self) -> dict[int, dict[str, Any]]:
        return {r['id']: r for r in self._rows('SELECT id, last_ok_at, last_error, consecutive_failures FROM sources')}

    def upsert_source(self, source_id: int | None, name: str, feed_url: str, weight: float, max_items: int, fetch_body: bool) -> None:
        with self._conn() as conn:
            if source_id:
                conn.execute(
                    'UPDATE sources SET name=?, feed_url=?, weight=?, max_items=?, fetch_body=? WHERE id=?',
                    (name, feed_url, weight, max_items, int(fetch_body), source_id),
                )
            else:
                conn.execute(
                    'INSERT INTO sources(name, feed_url, weight, max_items, fetch_body) VALUES (?,?,?,?,?)',
                    (name, feed_url, weight, max_items, int(fetch_body)),
                )

    def delete_source(self, source_id: int) -> None:
        with self._conn() as conn:
            conn.execute('DELETE FROM sources WHERE id=?', (source_id,))

    def set_source_enabled(self, source_id: int, enabled: bool) -> None:
        with self._conn() as conn:
            conn.execute('UPDATE sources SET enabled=? WHERE id=?', (int(enabled), source_id))

    def record_source_result(self, run_id: int, source_id: int, count: int, error: str) -> None:
        with self._conn() as conn:
            conn.execute('INSERT INTO source_results VALUES (?,?,?,?)', (run_id, source_id, count, error))
            if count and not error:
                conn.execute(
                    "UPDATE sources SET last_ok_at=?, last_error='', consecutive_failures=0 WHERE id=?",
                    (_now(), source_id),
                )
            else:
                conn.execute(
                    'UPDATE sources SET last_error=?, consecutive_failures=consecutive_failures+1 WHERE id=?',
                    (error, source_id),
                )

    # ---------- runs ----------
    def start_run(self, trigger: str) -> int:
        with self._conn() as conn:
            conn.execute("UPDATE runs SET status='stale', finished_at=? WHERE status='running'", (_now(),))
            cursor = conn.execute("INSERT INTO runs(trigger, status, started_at) VALUES (?, 'running', ?)", (trigger, _now()))
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict, error: str = '') -> None:
        with self._conn() as conn:
            conn.execute(
                'UPDATE runs SET status=?, finished_at=?, stats_json=?, error=? WHERE id=?',
                (status, _now(), json.dumps(stats, ensure_ascii=False), error, run_id),
            )

    def list_runs(self, limit: int = 30) -> list[dict]:
        rows = self._rows('SELECT * FROM runs ORDER BY id DESC LIMIT ?', (limit,))
        for row in rows:
            row['stats'] = json.loads(row.pop('stats_json'))
        return rows

    def get_run(self, run_id: int) -> dict | None:
        rows = self._rows('SELECT * FROM runs WHERE id=?', (run_id,))
        if not rows:
            return None
        run = rows[0]
        run['stats'] = json.loads(run.pop('stats_json'))
        run['sources'] = self._rows(
            'SELECT r.*, s.name FROM source_results r LEFT JOIN sources s ON s.id=r.source_id WHERE run_id=?', (run_id,)
        )
        return run

    def latest_completed_run(self) -> dict | None:
        rows = self._rows("SELECT * FROM runs WHERE status='completed' ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None

    # ---------- events ----------
    def save_events(self, run_id: int, events: list[Event]) -> None:
        with self._conn() as conn:
            for rank, event in enumerate(events, 1):
                analysis = event.lead_analysis
                conn.execute(
                    'INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        event.id, run_id, rank, event.score,
                        event.summary.headline if event.summary else event.lead.title,
                        analysis.region.value if analysis else '',
                        analysis.topic.value if analysis else '',
                        int(event.summary_degraded), int(event.is_update),
                        event.model_dump_json(), _now(),
                    ),
                )
                for article in event.articles:
                    for source in [article.source, *article.duplicate_sources]:
                        conn.execute('INSERT INTO event_urls VALUES (?,?,?,?)', (run_id, event.id, article.url, source))

    def load_events(self, run_id: int) -> list[Event]:
        rows = self._rows('SELECT payload_json FROM events WHERE run_id=? ORDER BY rank', (run_id,))
        return [Event.model_validate_json(r['payload_json']) for r in rows]

    def recent_event_index(self, hours: float = 48, exclude_run: int | None = None) -> dict[str, dict]:
        """近期已发布事件：url → {event_key, sources, urls}，用于跨批次判断新事件 / 更新 / 重复。"""
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        rows = self._rows(
            'SELECT u.run_id, u.event_id, u.url, u.source FROM event_urls u JOIN runs r ON r.id=u.run_id '
            "WHERE r.status='completed' AND r.started_at >= ? AND u.run_id != ?",
            (since, exclude_run or -1),
        )
        groups: dict[str, dict] = {}
        for row in rows:
            key = f"{row['run_id']}:{row['event_id']}"
            group = groups.setdefault(key, {'event_key': key, 'sources': set(), 'urls': set()})
            group['sources'].add(row['source'])
            group['urls'].add(row['url'])
        return {url: group for group in groups.values() for url in group['urls']}

    # ---------- llm calls ----------
    def record_llm_calls(self, run_id: int, calls: list[LLMCall]) -> None:
        with self._conn() as conn:
            conn.executemany(
                'INSERT INTO llm_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                [
                    (run_id, c.stage, c.model, c.prompt_version, c.input_hash, c.attempts, c.prompt_tokens,
                     c.completion_tokens, c.elapsed_ms, int(c.cached), c.schema_mode, c.error)
                    for c in calls
                ],
            )

    def llm_stats(self, run_id: int) -> dict[str, dict[str, int]]:
        rows = self._rows(
            'SELECT stage, COUNT(*) AS calls, SUM(cached) AS cached, SUM(prompt_tokens) AS prompt_tokens, '
            'SUM(completion_tokens) AS completion_tokens, '
            'SUM(CASE WHEN attempts > 1 THEN 1 ELSE 0 END) AS retries, '
            "SUM(CASE WHEN error != '' THEN 1 ELSE 0 END) AS errors "
            'FROM llm_calls WHERE run_id=? GROUP BY stage',
            (run_id,),
        )
        return {r.pop('stage'): {k: int(v or 0) for k, v in r.items()} for r in rows}

    # ---------- channels ----------
    def list_channels(self, enabled_only: bool = False) -> list[dict]:
        rows = self._rows('SELECT * FROM channels' + (' WHERE enabled=1' if enabled_only else '') + ' ORDER BY id')
        for row in rows:
            row['config'] = json.loads(row.pop('config_json'))
        return rows

    def get_channel(self, channel_id: int) -> dict | None:
        return next((c for c in self.list_channels() if c['id'] == channel_id), None)

    def upsert_channel(self, channel_id: int | None, kind: str, name: str, config: dict, enabled: bool = True) -> None:
        with self._conn() as conn:
            if channel_id:
                conn.execute(
                    'UPDATE channels SET kind=?, name=?, config_json=?, enabled=? WHERE id=?',
                    (kind, name, json.dumps(config, ensure_ascii=False), int(enabled), channel_id),
                )
            else:
                conn.execute(
                    'INSERT INTO channels(kind, name, config_json, enabled) VALUES (?,?,?,?)',
                    (kind, name, json.dumps(config, ensure_ascii=False), int(enabled)),
                )

    def delete_channel(self, channel_id: int) -> None:
        with self._conn() as conn:
            conn.execute('DELETE FROM channels WHERE id=?', (channel_id,))

    def record_delivery(self, run_id: int, channel_id: int, status: str, error: str = '') -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO deliveries(run_id, channel_id, status, error, created_at) VALUES (?,?,?,?,?)',
                (run_id, channel_id, status, error, _now()),
            )

    def deliveries(self, run_id: int) -> list[dict]:
        return self._rows(
            'SELECT d.*, c.name FROM deliveries d LEFT JOIN channels c ON c.id=d.channel_id WHERE run_id=?', (run_id,)
        )

    # ---------- settings kv ----------
    def get_setting(self, key: str, default: str = '') -> str:
        rows = self._rows('SELECT value FROM settings WHERE key=?', (key,))
        return rows[0]['value'] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value)
            )
