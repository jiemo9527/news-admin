"""SQLite 持久化。事件以 JSON 快照保存，便于复现与评测。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import tz
from .fetch import Source
from .llm import LLMCall
from .models import Event

FEED_RANGES = {'all': '全部', 'today': '今天', 'yesterday': '昨天', '3d': '3天内', '7d': '一周内'}
FEED_SORTS = {'time': '最新', 'score': '评级'}


@dataclass
class FeedItem:
    run_id: int
    event: Event


@dataclass
class FeedPage:
    items: list[FeedItem]
    total: int
    page: int
    per_page: int

    @property
    def events(self) -> list[Event]:
        return [item.event for item in self.items]

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))


def _event_time(event: Event, fallback: str) -> str:
    """首页排序/筛选用的时间：事件最近一次报道（或页面更新）的时间，没有则用入库时间。"""
    latest = event.last_updated
    return tz.iso(latest) if latest else fallback


def _search_text(event: Event) -> str:
    parts = [event.summary.headline if event.summary else '', *(s.text for s in (event.summary.sentences if event.summary else []))]
    parts += [a.title for a in event.articles] + event.sources
    parts += [e for an in event.analyses.values() for e in [an.title_zh, *an.entities]]
    return '\n'.join(parts).lower()


def _sources_text(event: Event) -> str:
    return '|' + '|'.join(event.sources) + '|'


def _like_escape(term: str) -> str:
    return term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

PUSH_BRIEFS_SCHEMA = """CREATE TABLE IF NOT EXISTS push_briefs (
    id TEXT PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    feed_url TEXT NOT NULL UNIQUE,
    weight REAL NOT NULL DEFAULT 1.0,
    max_items INTEGER NOT NULL DEFAULT 20,
    fetch_body INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL DEFAULT 'rss',
    link_pattern TEXT NOT NULL DEFAULT '',
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
CREATE TABLE IF NOT EXISTS access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT NOT NULL,
    event TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 1,
    first_at TEXT NOT NULL,
    last_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_access_last ON access_log(last_at);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    enabled INTEGER NOT NULL DEFAULT 1,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    last_ip TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT
);
"""


def _now() -> str:
    return tz.iso()


_TIME_COLUMNS = {
    'runs': ('started_at', 'finished_at'), 'events': ('created_at',), 'deliveries': ('created_at',), 'sources': ('last_ok_at',),
}


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.executescript(PUSH_BRIEFS_SCHEMA)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(sources)')}
            if 'kind' not in columns:
                conn.execute("ALTER TABLE sources ADD COLUMN kind TEXT NOT NULL DEFAULT 'rss'")
            if 'link_pattern' not in columns:
                conn.execute("ALTER TABLE sources ADD COLUMN link_pattern TEXT NOT NULL DEFAULT ''")
            user_columns = {row[1] for row in conn.execute('PRAGMA table_info(users)')}
            for column, ddl in (('last_ip', "TEXT NOT NULL DEFAULT ''"), ('last_seen_at', 'TEXT')):
                if column not in user_columns:
                    conn.execute(f'ALTER TABLE users ADD COLUMN {column} {ddl}')
            event_columns = {row[1] for row in conn.execute('PRAGMA table_info(events)')}
            for column, ddl in (('canonical_id', "TEXT NOT NULL DEFAULT ''"), ('event_time', 'TEXT'), ('search_text', "TEXT NOT NULL DEFAULT ''"),
                                ('in_brief', 'INTEGER NOT NULL DEFAULT 1'), ('sources_text', "TEXT NOT NULL DEFAULT ''")):
                if column not in event_columns:
                    conn.execute(f'ALTER TABLE events ADD COLUMN {column} {ddl}')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_events_canonical ON events(canonical_id, run_id)')
            # 早期版本按 UTC 存储时间戳，统一改写为北京时间（字符串比较依赖同一时区）
            for table, cols in _TIME_COLUMNS.items():
                for col in cols:
                    rows = conn.execute(f"SELECT rowid, {col} FROM {table} WHERE {col} LIKE '%+00:00'").fetchall()
                    for rowid, value in rows:
                        conn.execute(f'UPDATE {table} SET {col}=? WHERE rowid=?', (tz.iso(tz.parse(value)), rowid))
        self._backfill_feed_columns()

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
        """写入默认来源；用户在后台删除过的默认来源不再补回。"""
        removed = set(json.loads(self.get_setting('removed_sources', '[]')))
        with self._conn() as conn:
            for s in sources:
                if s['feed_url'] in removed:
                    continue
                conn.execute(
                    'INSERT OR IGNORE INTO sources(name, feed_url, weight, kind, link_pattern, max_items, fetch_body) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (s['name'], s['feed_url'], s.get('weight', 1.0), s.get('kind', 'rss'), s.get('link_pattern', ''), s.get('max_items', 10),
                     int(s.get('fetch_body', True))),
                )

    def list_sources(self, enabled_only: bool = False) -> list[Source]:
        sql = 'SELECT * FROM sources' + (' WHERE enabled = 1' if enabled_only else '') + ' ORDER BY id'
        return [
            Source(
                id=r['id'], name=r['name'], feed_url=r['feed_url'], weight=r['weight'],
                max_items=r['max_items'], fetch_body=bool(r['fetch_body']), enabled=bool(r['enabled']),
                kind=r['kind'], link_pattern=r['link_pattern'],
            )
            for r in self._rows(sql)
        ]

    def source_health(self) -> dict[int, dict[str, Any]]:
        return {r['id']: r for r in self._rows('SELECT id, last_ok_at, last_error, consecutive_failures FROM sources')}

    def upsert_source(
        self, source_id: int | None, name: str, feed_url: str, weight: float, max_items: int, fetch_body: bool,
        kind: str = 'rss', link_pattern: str = '',
    ) -> None:
        values = (name, feed_url, weight, max_items, int(fetch_body), kind, link_pattern)
        with self._conn() as conn:
            if source_id:
                conn.execute(
                    'UPDATE sources SET name=?, feed_url=?, weight=?, max_items=?, fetch_body=?, kind=?, link_pattern=? WHERE id=?',
                    (*values, source_id),
                )
            else:
                conn.execute(
                    'INSERT INTO sources(name, feed_url, weight, max_items, fetch_body, kind, link_pattern) VALUES (?,?,?,?,?,?,?)',
                    values,
                )

    def delete_source(self, source_id: int) -> None:
        rows = self._rows('SELECT feed_url FROM sources WHERE id=?', (source_id,))
        if rows:
            removed = set(json.loads(self.get_setting('removed_sources', '[]'))) | {rows[0]['feed_url']}
            self.set_setting('removed_sources', json.dumps(sorted(removed)))
        with self._conn() as conn:
            conn.execute('DELETE FROM sources WHERE id=?', (source_id,))

    def add_preset_source(self, preset: dict) -> None:
        removed = set(json.loads(self.get_setting('removed_sources', '[]'))) - {preset['feed_url']}
        self.set_setting('removed_sources', json.dumps(sorted(removed)))
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO sources(name, feed_url, weight, kind, link_pattern, max_items, fetch_body, enabled) VALUES (?,?,?,?,?,?,?,1) '
                'ON CONFLICT(feed_url) DO UPDATE SET enabled=1',
                (preset['name'], preset['feed_url'], preset.get('weight', 1.0), preset.get('kind', 'rss'),
                 preset.get('link_pattern', ''), preset.get('max_items', 10), int(preset.get('fetch_body', True))),
            )

    def delete_run(self, run_id: int) -> bool:
        """删除一期简报（运行记录、事件、链接索引、推送记录一并删除）。"""
        with self._conn() as conn:
            conn.execute('DELETE FROM event_urls WHERE run_id=?', (run_id,))
            return conn.execute("DELETE FROM runs WHERE id=? AND status != 'running'", (run_id,)).rowcount > 0

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
    def _canonical_id(self, conn, run_id: int, event: Event) -> str:
        """同一事件的多个版本共用一个 canonical_id，首页只显示最新版本；后续进展（follow_up）是新事件，单独成条。

        判定为同一事件：① 流水线标记为「更新」并引用了上一版本；② 与更早的事件共享任何一篇报道链接。
        """
        ref = event.previous[0] if event.previous else None
        if event.is_update and ref and ref.relation == 'update':
            row = conn.execute('SELECT canonical_id FROM events WHERE run_id=? AND id=?', (ref.run_id, ref.event_id)).fetchone()
            return row[0] if row and row[0] else f'{ref.run_id}:{ref.event_id}'
        urls = list({a.url for a in event.articles})
        if urls:
            marks = ','.join('?' * len(urls))
            row = conn.execute(
                f"SELECT e.canonical_id FROM event_urls u JOIN events e ON e.run_id=u.run_id AND e.id=u.event_id "
                f"WHERE u.url IN ({marks}) AND u.run_id < ? AND e.canonical_id != '' ORDER BY u.run_id DESC LIMIT 1",
                (*urls, run_id),
            ).fetchone()
            if row:
                return row[0]
        return f'{run_id}:{event.id}'

    def save_events(self, run_id: int, events: list[Event]) -> None:
        with self._conn() as conn:
            created = _now()
            for rank, event in enumerate(events, 1):
                analysis = event.lead_analysis
                conn.execute(
                    'INSERT INTO events(id, run_id, rank, score, headline, region, topic, degraded, is_update, payload_json, created_at, '
                    'canonical_id, event_time, search_text, in_brief, sources_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (
                        event.id, run_id, rank, event.score,
                        event.summary.headline if event.summary else event.lead.title,
                        analysis.region.value if analysis else '',
                        analysis.topic.value if analysis else '',
                        int(event.summary_degraded), int(event.is_update),
                        event.model_dump_json(), created,
                        self._canonical_id(conn, run_id, event), _event_time(event, created), _search_text(event),
                        int(event.in_brief), _sources_text(event),
                    ),
                )
                for article in event.articles:
                    for source in [article.source, *article.duplicate_sources]:
                        conn.execute('INSERT INTO event_urls VALUES (?,?,?,?)', (run_id, event.id, article.url, source))

    FEED_INDEX_VERSION = '2'

    def _backfill_feed_columns(self) -> None:
        """补齐首页索引列；索引规则升级时（版本号变化）全部重算。"""
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='feed_index_version'").fetchone()
            rebuild = not row or row[0] != self.FEED_INDEX_VERSION
            if rebuild:
                conn.execute("UPDATE events SET canonical_id=''")
            rows = conn.execute(
                "SELECT rowid, run_id, payload_json, created_at FROM events "
                "WHERE canonical_id='' OR event_time IS NULL OR search_text='' OR sources_text='' ORDER BY run_id, rank"
            ).fetchall()
            for rowid, run_id, payload, created in rows:
                event = Event.model_validate_json(payload)
                conn.execute(
                    'UPDATE events SET canonical_id=?, event_time=?, search_text=?, sources_text=? WHERE rowid=?',
                    (self._canonical_id(conn, run_id, event), _event_time(event, created), _search_text(event), _sources_text(event), rowid),
                )
            conn.execute(
                "INSERT INTO settings VALUES ('feed_index_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (self.FEED_INDEX_VERSION,),
            )

    _LATEST_JOIN = (
        'FROM events e JOIN runs r ON r.id=e.run_id '
        "JOIN (SELECT e2.canonical_id, MAX(e2.run_id) AS run_id FROM events e2 JOIN runs r2 ON r2.id=e2.run_id "
        "WHERE r2.status='completed' GROUP BY e2.canonical_id) latest "
        'ON latest.canonical_id=e.canonical_id AND latest.run_id=e.run_id '
    )

    @staticmethod
    def _feed_where(range_: str, q: str, topic: str, now: datetime, source: str = '') -> tuple[list[str], list]:
        where, params = ["r.status='completed'"], []
        if range_ == 'today':
            where.append('e.event_time >= ?'); params.append(tz.iso(tz.day_start(now)))
        elif range_ == 'yesterday':
            where.append('e.event_time >= ? AND e.event_time < ?'); params += [tz.iso(tz.day_start(now, 1)), tz.iso(tz.day_start(now))]
        elif range_ == '3d':
            where.append('e.event_time >= ?'); params.append(tz.iso(tz.day_start(now, 2)))
        elif range_ == '7d':
            where.append('e.event_time >= ?'); params.append(tz.iso(tz.day_start(now, 6)))
        if topic:
            where.append('e.topic = ?'); params.append(topic)
        if source:
            where.append("e.sources_text LIKE ? ESCAPE '\\'"); params.append(f'%|{_like_escape(source)}|%')
        for term in q.lower().split()[:8]:
            where.append("e.search_text LIKE ? ESCAPE '\\'"); params.append(f'%{_like_escape(term)}%')
        return where, params

    def feed(
        self, range_: str = 'all', q: str = '', page: int = 1, per_page: int = 20, sort: str = 'time', now: datetime | None = None,
        topic: str = '', source: str = '',
    ) -> FeedPage:
        """首页汇总：所有已完成运行的事件，同一事件只取最新版本；按北京时间自然日筛选，关键词需全部命中。"""
        now = tz.to_local(now) if now else tz.now()
        where, params = self._feed_where(range_, q, topic, now, source)
        base = self._LATEST_JOIN + 'WHERE ' + ' AND '.join(where)
        order = 'e.score DESC, e.event_time DESC' if sort == 'score' else 'e.event_time DESC, e.score DESC'
        page = max(1, page)
        with self._conn() as conn:
            total = conn.execute(f'SELECT COUNT(*) {base}', params).fetchone()[0]
            rows = conn.execute(
                f'SELECT e.run_id, e.payload_json {base} ORDER BY {order} LIMIT ? OFFSET ?', (*params, per_page, (page - 1) * per_page)
            ).fetchall()
        items = [FeedItem(run_id=int(r[0]), event=Event.model_validate_json(r[1])) for r in rows]
        return FeedPage(items=items, total=int(total), page=page, per_page=per_page)

    def topic_counts(self, range_: str = 'all', q: str = '', now: datetime | None = None, source: str = '') -> dict[str, int]:
        now = tz.to_local(now) if now else tz.now()
        where, params = self._feed_where(range_, q, '', now, source)
        rows = self._rows(
            f"SELECT e.topic, COUNT(*) AS n {self._LATEST_JOIN} WHERE {' AND '.join(where)} AND e.topic != '' GROUP BY e.topic",
            tuple(params),
        )
        return {r['topic']: r['n'] for r in rows}

    def source_counts(self, range_: str = 'all', q: str = '', topic: str = '', now: datetime | None = None) -> dict[str, int]:
        """首页「来源」下拉框：当前筛选条件下每个来源有多少条事件。"""
        now = tz.to_local(now) if now else tz.now()
        where, params = self._feed_where(range_, q, topic, now)
        rows = self._rows(f"SELECT e.sources_text {self._LATEST_JOIN} WHERE {' AND '.join(where)}", tuple(params))
        counts: dict[str, int] = {}
        for row in rows:
            for name in filter(None, row['sources_text'].split('|')):
                counts[name] = counts.get(name, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def brief_index(self, limit: int = 100) -> list[dict]:
        return self._rows(
            "SELECT r.id, r.trigger, r.started_at, r.finished_at, "
            "(SELECT COUNT(*) FROM events WHERE run_id=r.id AND in_brief=1) AS events, "
            "(SELECT headline FROM events WHERE run_id=r.id ORDER BY rank LIMIT 1) AS top "
            "FROM runs r WHERE r.status='completed' ORDER BY r.id DESC LIMIT ?",
            (limit,),
        )

    def cleanup(self, retention_days: int = 90, now: datetime | None = None) -> int:
        """删除超过保留期的运行（事件、来源结果、推送记录随外键级联删除）。返回删除的运行数。"""
        cutoff = tz.iso((tz.to_local(now) if now else tz.now()) - timedelta(days=retention_days))
        with self._conn() as conn:
            conn.execute('DELETE FROM access_log WHERE last_at < ?', (cutoff,))
            return conn.execute("DELETE FROM runs WHERE started_at < ? AND status != 'running'", (cutoff,)).rowcount

    def load_events(self, run_id: int, brief_only: bool = True) -> list[Event]:
        """brief_only=True：只取进入该期简报的事件；False：该次运行入库的全部事件。"""
        sql = 'SELECT payload_json FROM events WHERE run_id=?' + (' AND in_brief=1' if brief_only else '') + ' ORDER BY rank'
        rows = self._rows(sql, (run_id,))
        return [Event.model_validate_json(r['payload_json']) for r in rows]

    def recent_events(self, days: float = 7, exclude_run: int | None = None) -> list[tuple[int, Event]]:
        """近 N 天已完成运行中发布的事件，按运行从新到旧；同一故事线只保留最新一次。"""
        since = tz.iso(tz.now() - timedelta(days=days))
        rows = self._rows(
            'SELECT e.run_id, e.payload_json FROM events e JOIN runs r ON r.id=e.run_id '
            "WHERE r.status='completed' AND r.started_at >= ? AND e.run_id != ? ORDER BY e.run_id DESC, e.rank",
            (since, exclude_run or -1),
        )
        result: list[tuple[int, Event]] = []
        seen_stories: set[str] = set()
        for row in rows:
            event = Event.model_validate_json(row['payload_json'])
            story = event.story_id or event.id
            if story in seen_stories:
                continue
            seen_stories.add(story)
            result.append((int(row['run_id']), event))
        return result

    def recent_event_index(self, hours: float = 48, exclude_run: int | None = None) -> dict[str, dict]:
        """近期已发布事件：url → {event_key, sources, urls}，用于跨批次判断新事件 / 更新 / 重复。"""
        since = tz.iso(tz.now() - timedelta(hours=hours))
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

    # ---------- users ----------
    def list_users(self) -> list[dict]:
        return self._rows('SELECT id, username, role, enabled, created_at, last_login_at, last_ip, last_seen_at FROM users ORDER BY id')

    def get_user(self, user_id: int) -> dict | None:
        rows = self._rows('SELECT * FROM users WHERE id=?', (user_id,))
        return rows[0] if rows else None

    def get_user_by_name(self, username: str) -> dict | None:
        rows = self._rows('SELECT * FROM users WHERE username=?', (username,))
        return rows[0] if rows else None

    def create_user(self, username: str, password_hash: str, role: str) -> int:
        with self._conn() as conn:
            return int(conn.execute(
                'INSERT INTO users(username, password_hash, role, created_at) VALUES (?,?,?,?)', (username, password_hash, role, _now())
            ).lastrowid)

    def update_user(self, user_id: int, **fields) -> None:
        """可改 password_hash / role / enabled；改密码、角色、停用都会让该用户已登录的会话失效。"""
        allowed = {k: v for k, v in fields.items() if k in ('password_hash', 'role', 'enabled')}
        if not allowed:
            return
        sets = ', '.join(f'{k}=?' for k in allowed) + ', session_version=session_version+1'
        with self._conn() as conn:
            conn.execute(f'UPDATE users SET {sets} WHERE id=?', (*allowed.values(), user_id))

    def touch_login(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute('UPDATE users SET last_login_at=? WHERE id=?', (_now(), user_id))

    VISIT_MERGE_MINUTES = 30

    def log_access(self, user_id: int | None, username: str, event: str, ip: str, user_agent: str = '', path: str = '') -> None:
        """记录登录/登出/登录失败/页面访问。同一用户同一 IP 在 30 分钟内的连续访问合并为一行（累计次数、最后页面）。"""
        now = _now()
        agent = user_agent[:200]
        with self._conn() as conn:
            if event == 'visit':
                since = tz.iso(tz.now() - timedelta(minutes=self.VISIT_MERGE_MINUTES))
                row = conn.execute(
                    "SELECT id FROM access_log WHERE user_id=? AND ip=? AND event='visit' AND last_at >= ? ORDER BY id DESC LIMIT 1",
                    (user_id, ip, since),
                ).fetchone()
                if row:
                    conn.execute('UPDATE access_log SET hits=hits+1, last_at=?, path=?, user_agent=? WHERE id=?', (now, path[:200], agent, row[0]))
                else:
                    conn.execute(
                        'INSERT INTO access_log(user_id, username, event, ip, user_agent, path, first_at, last_at) VALUES (?,?,?,?,?,?,?,?)',
                        (user_id, username, event, ip, agent, path[:200], now, now),
                    )
                conn.execute('UPDATE users SET last_seen_at=?, last_ip=? WHERE id=?', (now, ip, user_id))
                return
            conn.execute(
                'INSERT INTO access_log(user_id, username, event, ip, user_agent, path, first_at, last_at) VALUES (?,?,?,?,?,?,?,?)',
                (user_id, username[:64], event, ip, agent, path[:200], now, now),
            )
            if event == 'login' and user_id:
                conn.execute('UPDATE users SET last_ip=?, last_seen_at=? WHERE id=?', (ip, now, user_id))

    def list_access(self, limit: int = 200, user_id: int | None = None) -> list[dict]:
        sql = 'SELECT * FROM access_log' + (' WHERE user_id=?' if user_id else '') + ' ORDER BY last_at DESC, id DESC LIMIT ?'
        return self._rows(sql, ((user_id, limit) if user_id else (limit,)))

    def delete_user(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute('DELETE FROM users WHERE id=?', (user_id,))

    def save_push_brief(self, run_id: int, events: list[Event]) -> str:
        import secrets
        key = secrets.token_hex(12)
        with self._conn() as conn:
            conn.execute('INSERT INTO push_briefs(id,run_id,created_at,payload_json) VALUES (?,?,?,?)',
                         (key, run_id, _now(), json.dumps([e.model_dump(mode='json') for e in events], ensure_ascii=False)))
        return key

    def get_push_brief(self, key: str) -> dict | None:
        rows = self._rows('SELECT * FROM push_briefs WHERE id=?', (key,))
        if not rows:
            return None
        row = rows[0]
        row['events'] = [Event.model_validate(e) for e in json.loads(row.pop('payload_json'))]
        return row

    # ---------- settings kv ----------
    def get_setting(self, key: str, default: str = '') -> str:
        rows = self._rows('SELECT value FROM settings WHERE key=?', (key,))
        return rows[0]['value'] if rows else default

    def set_setting(self, key: str, value: str) -> None:
        with self._conn() as conn:
            conn.execute(
                'INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value)
            )
