from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import Paths


def ensure_dirs() -> None:
    for path in [Paths.data_dir, Paths.artifacts_dir, Paths.publications_dir, Paths.exports_dir, Paths.thumbnails_dir]:
        path.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(Paths.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=30000')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA = '''
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    url TEXT NOT NULL,
    rss_url TEXT NOT NULL DEFAULT '',
    selectors_json TEXT NOT NULL,
    include_patterns_json TEXT NOT NULL,
    exclude_patterns_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    notes TEXT NOT NULL DEFAULT '',
    last_success_at TEXT,
    last_error_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS platforms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    config_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_id INTEGER NOT NULL REFERENCES platforms(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    target_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    timezone TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS run_source_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    fetched_count INTEGER NOT NULL DEFAULT 0,
    kept_count INTEGER NOT NULL DEFAULT 0,
    raw_relpath TEXT NOT NULL DEFAULT '',
    error_text TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS story_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    canonical_key TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    source_count INTEGER NOT NULL DEFAULT 0,
    summary_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(run_id, canonical_key)
);
CREATE TABLE IF NOT EXISTS story_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES story_groups(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    snippet TEXT NOT NULL DEFAULT '',
    rank_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    relpath TEXT NOT NULL,
    latest_relpath TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    publication_id INTEGER NOT NULL REFERENCES publications(id) ON DELETE CASCADE,
    recipient_id INTEGER NOT NULL REFERENCES recipients(id) ON DELETE CASCADE,
    platform_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    request_json TEXT NOT NULL DEFAULT '{}',
    response_json TEXT NOT NULL DEFAULT '{}',
    error_text TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(idempotency_key)
);
'''


def init_db() -> None:
    with db_cursor() as conn:
        conn.executescript(SCHEMA)
