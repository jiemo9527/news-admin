"""手动备份/恢复与清空。不会自动创建备份，也不会解压到任意路径。"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

from dotenv import dotenv_values

from . import tz
from .config import Settings, update_env_file
from .models import Event
from .store import Store, PUSH_BRIEFS_SCHEMA

MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
CONFIG_FIELDS = (
    'llm_base_url', 'llm_model', 'llm_api_key', 'llm_concurrency',
    'embedding_base_url', 'embedding_model', 'embedding_api_key',
    'cluster_high', 'cluster_low', 'cluster_window_hours', 'min_topic_confidence',
    'min_impact', 'drop_opinion', 'max_events', 'public_base_url', 'trust_proxy',
)


@contextmanager
def exclusive(rt):
    if not rt.operation_lock.acquire(blocking=False):
        raise RuntimeError('任务正在运行，请稍后重试')
    acquired = rt.pipeline._lock.acquire(blocking=False)
    try:
        if not acquired:
            raise RuntimeError('抓取正在运行，请稍后重试')
        yield
    finally:
        if acquired:
            rt.pipeline._lock.release()
        rt.operation_lock.release()


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _config_bytes(rt):
    # 实际 .env 优先；不包含数据目录、启动密码或其他任意环境变量。
    saved = dotenv_values(rt.settings.env_path) if rt.settings.env_path.exists() else {}
    values = {}
    for field in CONFIG_FIELDS:
        key = 'NEWS_' + field.upper()
        value = saved.get(key, getattr(rt.settings, field))
        if value is not None:
            values[key] = str(value).lower() if isinstance(value, bool) else str(value)
    from .config import _quote
    return ('\n'.join(f'{k}={_quote(v)}' for k, v in values.items()) + '\n').encode('utf-8')


def export_backup(rt) -> bytes:
    with exclusive(rt):
        root = rt.settings.data_dir
        with tempfile.TemporaryDirectory(prefix='news-export-', dir=root) as tmp:
            snapshot = Path(tmp) / 'news.db'
            with closing(sqlite3.connect(rt.store.path)) as src, closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
            files = {'news.db': snapshot.read_bytes(), 'config.env': _config_bytes(rt)}
        for path in (root / 'thumbs').glob('*.webp'):
            if len(path.stem) == 32 and all(c in '0123456789abcdef' for c in path.stem):
                files['thumbs/' + path.name] = path.read_bytes()
        if len(files) + 1 > 20000 or sum(len(data) for data in files.values()) > MAX_EXPANDED - 8 * 1024 * 1024:
            raise ValueError('数据超过备份包限制，请先清理过期新闻或缩略图')
        manifest = {'format': 'news-admin-backup', 'version': 1, 'created_at': tz.iso(),
                    'files': {name: {'sha256': _digest(data), 'size': len(data)} for name, data in files.items()}}
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
            for name, data in files.items():
                z.writestr(name, data)
        payload = out.getvalue()
        if len(payload) > MAX_ARCHIVE:
            raise ValueError('备份包超过 256 MB，请先清理过期新闻或缩略图')
        return payload


def _validated_files(payload):
    if len(payload) > MAX_ARCHIVE:
        raise ValueError('备份包超过 256 MB')
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            infos = z.infolist()
            names = [i.filename for i in infos]
            if len(names) != len(set(names)) or len(names) > 20000 or sum(i.file_size for i in infos) > MAX_EXPANDED:
                raise ValueError('备份包大小或文件列表不合法')
            for name in names:
                path = PurePosixPath(name)
                allowed = name in ('manifest.json', 'news.db', 'config.env') or (
                    len(path.parts) == 2 and path.parts[0] == 'thumbs' and path.suffix == '.webp'
                    and len(path.stem) == 32 and all(c in '0123456789abcdef' for c in path.stem))
                if not allowed or '\\' in name or path.is_absolute() or '..' in path.parts:
                    raise ValueError('备份包包含不允许的路径')
            manifest = json.loads(z.read('manifest.json'))
            if manifest.get('format') != 'news-admin-backup' or manifest.get('version') != 1:
                raise ValueError('备份格式或版本不支持')
            if set(manifest['files']) != set(names) - {'manifest.json'} or not {'news.db', 'config.env'} <= set(names):
                raise ValueError('备份包缺少必要文件')
            files = {n: z.read(n) for n in manifest['files']}
            for name, data in files.items():
                meta = manifest['files'][name]
                if _digest(data) != meta['sha256'] or len(data) != meta['size']:
                    raise ValueError('备份文件校验失败：' + name)
            return files
    except (zipfile.BadZipFile, KeyError, TypeError, json.JSONDecodeError, OSError, RuntimeError) as exc:
        raise ValueError('无效或损坏的备份包') from exc


def restore_backup(rt, payload: bytes) -> dict:
    with exclusive(rt):
        files = _validated_files(payload)
        root = rt.settings.data_dir
        with tempfile.TemporaryDirectory(prefix='news-restore-', dir=root) as tmp:
            snapshot = Path(tmp) / 'news.db'
            snapshot.write_bytes(files['news.db'])
            env = Path(tmp) / 'config.env'
            env.write_bytes(files['config.env'])
            old_settings = rt.settings
            old_env = rt.settings.env_path.read_bytes() if rt.settings.env_path.exists() else None
            # 内存中的事务回滚状态，仅存活到本次操作结束；不产生自动备份文件。
            old_images = None
            changed = False
            with closing(sqlite3.connect(':memory:')) as rollback:
                try:
                    restored_settings = Settings(_env_file=env)
                    from apscheduler.triggers.cron import CronTrigger
                    with closing(sqlite3.connect(snapshot)) as src, closing(sqlite3.connect(rt.store.path)) as live:
                        if src.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                            raise ValueError('数据库完整性检查失败')
                        expected = {r[0]: r[1] for r in live.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                        # v2 旧备份缺少新增的推送汇总表；只添加这一已知结构，再做严格比对。
                        src.executescript(PUSH_BRIEFS_SCHEMA)
                        actual = {r[0]: r[1] for r in src.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                        if actual != expected or src.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('trigger','view')").fetchone()[0]:
                            raise ValueError('数据库结构不兼容')
                        if src.execute('PRAGMA foreign_key_check').fetchall():
                            raise ValueError('数据库关联数据损坏')
                        if not src.execute("SELECT 1 FROM users WHERE role='admin' AND enabled=1").fetchone():
                            raise ValueError('备份中没有可登录的管理员')
                        for row in src.execute('SELECT payload_json FROM events'):
                            Event.model_validate_json(row[0])
                        for payload, in src.execute('SELECT payload_json FROM push_briefs'):
                            for event in json.loads(payload):
                                Event.model_validate(event)
                        saved = dict(src.execute('SELECT key,value FROM settings'))
                        for key, default in (('schedule_cron', '0 */2 * * *'), ('push_cron', '0 6,20 * * *')):
                            CronTrigger.from_crontab(saved.get(key, default), timezone=tz.TZ)
                        if not 7 <= int(saved.get('retention_days', '90')) <= 365:
                            raise ValueError('历史保留天数不合法')
                        if src.execute("SELECT 1 FROM users WHERE role NOT IN ('admin','viewer')").fetchone():
                            raise ValueError('账号角色不合法')
                        result = {t: src.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('runs', 'events', 'users')}
                        old_max = live.execute('SELECT COALESCE(MAX(session_version),0) FROM users').fetchone()[0]
                        new_max = src.execute('SELECT COALESCE(MAX(session_version),0) FROM users').fetchone()[0]
                        # 每次恢复使用高于当前和备份的版本，避免旧 cookie 与恢复后账号 ID/版本碰撞。
                        src.execute('UPDATE users SET session_version=?', (max(old_max, new_max) + 1,))
                        src.execute("UPDATE runs SET status='stale', finished_at=? WHERE status='running'", (tz.iso(),))
                        src.commit()
                        live.backup(rollback)
                        old_images = {path.name: path.read_bytes() for path in (root / 'thumbs').iterdir() if path.is_file()}
                        changed = True
                        update_env_file(old_settings.env_path, {f'NEWS_{k.upper()}':
                            (None if getattr(restored_settings, k) is None else str(getattr(restored_settings, k))) for k in CONFIG_FIELDS})
                        for path in (root / 'thumbs').iterdir():
                            if path.is_file():
                                path.unlink()
                        for name, data in files.items():
                            if name.startswith('thumbs/'):
                                (root / name).write_bytes(data)
                        src.backup(live)
                    rt.store = Store(rt.store.path)
                    rt.pipeline.store = rt.store
                    rt.apply_settings(old_settings.model_copy(update={k: getattr(restored_settings, k) for k in CONFIG_FIELDS}))
                    return result
                except Exception as exc:
                    if changed:
                        with closing(sqlite3.connect(rt.store.path)) as live:
                            rollback.backup(live)
                        if old_env is None:
                            old_settings.env_path.unlink(missing_ok=True)
                        else:
                            old_settings.env_path.write_bytes(old_env)
                        for path in (root / 'thumbs').iterdir():
                            if path.is_file():
                                path.unlink()
                        for name, data in (old_images or {}).items():
                            (root / 'thumbs' / name).write_bytes(data)
                        rt.store = Store(rt.store.path)
                        rt.pipeline.store = rt.store
                        if rt.settings is not old_settings:
                            rt.apply_settings(old_settings)
                    raise ValueError(f'恢复失败：{type(exc).__name__}: {exc}') from exc


def clear_data(rt) -> dict:
    with exclusive(rt):
        with rt.store._conn() as conn:
            removed = conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
            conn.execute('DELETE FROM runs')
            for table in ('events', 'event_urls', 'source_results', 'llm_calls', 'deliveries', 'access_log'):
                conn.execute(f'DELETE FROM {table}')
            conn.execute("DELETE FROM users WHERE role != 'admin'")
            conn.execute('UPDATE users SET last_login_at=NULL, last_seen_at=NULL, last_ip=\'\', session_version=session_version+1')
            conn.execute("DELETE FROM settings WHERE key LIKE 'push_cursor_%' OR key LIKE 'admin_password%'")
            conn.execute("UPDATE sources SET last_ok_at=NULL, last_error='', consecutive_failures=0")
        for folder in (rt.settings.cache_dir, rt.thumbnails.directory):
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True, exist_ok=True)
        # LLM/embedding clients create their cache folders at construction.
        rt.apply_settings(rt.settings)
        return {'runs': removed}
